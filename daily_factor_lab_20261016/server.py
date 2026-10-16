"""Loopback-only API and compiled dashboard server. No external trading calls."""
import argparse
import json
import mimetypes
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from lab import ROOT, Config
from run_experiments import OUTPUTS, run_suite

STATE = {"busy": False, "progress": "就绪", "error": None, "latest_run": None}
LOCK = threading.Lock()
MARKET = None

def latest():
    index = OUTPUTS / "latest.json"
    if not index.exists(): return None
    pointer = json.loads(index.read_text(encoding="utf-8"))
    return json.loads((OUTPUTS / pointer["result"]).read_text(encoding="utf-8"))

def run_job(config):
    global MARKET
    try:
        def progress(s):
            with LOCK: STATE["progress"] = s
            print(s, flush=True)
        result, MARKET = run_suite(config, MARKET, progress=progress, dashboard_complete=True, build=True)
        with LOCK:
            STATE["latest_run"] = result["run_id"]
            STATE["error"] = (None if result["status"] == "complete" else "部分实验失败，请查看结果failures") or result.get("dashboard_build_error")
    except Exception as e:
        with LOCK: STATE["error"] = str(e)
        print(traceback.format_exc(), flush=True)
    finally:
        with LOCK: STATE["busy"] = False

class Handler(BaseHTTPRequestHandler):
    server_version = "DailyFactorLab/0.1"

    def payload(self, status, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def safe_file(self, root, relative, download=False):
        root = root.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return self.payload(404, {"error":"文件不存在"})
        if download and target.suffix.lower() not in {".csv", ".json", ".md", ".png"}:
            return self.payload(403,{"error":"不支持该文件类型"})
        data = target.read_bytes()
        self.send_response(200)
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") else ""))
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        if download: self.send_header("Content-Disposition",f'attachment; filename="{target.name}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/api/status":
            with LOCK: state = dict(STATE)
            index = OUTPUTS / "latest.json"
            if index.exists(): state["latest_run"] = json.loads(index.read_text(encoding="utf-8"))["run_id"]
            return self.payload(200,state)
        if path.path == "/api/result": return self.payload(200,latest() or {"status":"not_run", "summary":[], "curves":[], "yearly":[], "ic":[]})
        if path.path == "/api/download":
            relative = parse_qs(path.query).get("path",[""])[0]
            return self.safe_file(OUTPUTS,relative,True)
        if path.path.startswith("/api/"): return self.payload(404,{"error":"接口不存在"})
        return self.safe_file(ROOT / "dashboard" / "dist", path.path.lstrip("/") or "index.html")

    def do_POST(self):
        if urlparse(self.path).path != "/api/run": return self.payload(404,{"error":"接口不存在"})
        origin = self.headers.get("Origin")
        allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if origin and origin not in allowed: return self.payload(403,{"error":"只允许同源本地请求"})
        if not self.headers.get("Content-Type","").startswith("application/json"):
            return self.payload(415,{"error":"须提交JSON参数"})
        try:
            length = int(self.headers.get("Content-Length","0"))
            if not 0 < length <= 32768: raise ValueError("请求体大小不合法")
            obj = json.loads(self.rfile.read(length))
            values = obj.get("config",obj)
            if not isinstance(values,dict): raise ValueError("参数须为JSON对象")
            cfg = Config.from_dict(values)
        except Exception as e: return self.payload(400,{"error":str(e)})
        with LOCK:
            if STATE["busy"]: return self.payload(409,{"error":"已有回测正在运行"})
            STATE.update({"busy":True,"progress":"准备回测","error":None})
        threading.Thread(target=run_job,args=(cfg,),daemon=True).start()
        return self.payload(202,{"accepted":True,"config":cfg.__dict__})

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8776)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    print(f"Local daily research: http://127.0.0.1:{args.port}/ (Asia/Shanghai)",flush=True)
    server.serve_forever()
