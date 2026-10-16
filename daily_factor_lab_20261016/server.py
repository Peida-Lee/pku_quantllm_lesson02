"""Loopback-only API and compiled dashboard server. No external trading calls."""
import argparse
import json
import mimetypes
import re
import subprocess
import sys
import threading
import traceback
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from lab import ROOT, Config
from run_experiments import OUTPUTS, run_suite, make_snapshot, build_dashboard, save_json

STATE = {"busy": False, "progress": "就绪", "error": None, "latest_run": None}
LOCK = threading.Lock()
AI_LOCK = threading.Lock()
MARKET = None
STRATEGIES = ROOT / "strategies"
PACKS = ROOT / "block_packs"
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")

def read_documents(folder):
    if not folder.exists(): return []
    result = []
    for path in sorted(folder.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            result.append({"id":path.stem,"name":doc.get("name",path.stem),"document":doc})
        except (ValueError,OSError):
            continue
    return result

def catalog_response():
    from strategy_blocks import get_catalog
    packs = [r["document"] for r in read_documents(PACKS)]
    catalog = get_catalog(packs=packs)
    examples = []
    for row in read_documents(ROOT / "examples"):
        if row["document"].get("format") == "daily-factor-strategy":
            examples.append({"id":row["id"],"name":row["name"],"strategy":row["document"]})
    return {"catalog":catalog,"blocks":catalog["blocks"],"examples":examples,"packs":packs,
            "version":2,"import_formats":["daily-factor-strategy","daily-factor-block-pack"]}

def available_runs():
    rows=[]
    if not OUTPUTS.exists(): return rows
    for path in sorted(OUTPUTS.glob("run_*/result.json"),reverse=True):
        try:
            r=json.loads(path.read_text(encoding="utf-8"))
            rows.append({"run_id":r["run_id"],"generated_at":r["generated_at"],"config":r["config"],
                         "name":r.get("custom_strategy",{}).get("name") if r.get("custom_strategy") else "V1五因子实验",
                         "status":r.get("status"),"summary":r["summary"]})
        except (ValueError,OSError,KeyError): continue
    return rows

def latest():
    index = OUTPUTS / "latest.json"
    if not index.exists(): return None
    pointer = json.loads(index.read_text(encoding="utf-8"))
    return json.loads((OUTPUTS / pointer["result"]).read_text(encoding="utf-8"))


def research_job():
    global MARKET
    try:
        from research import run_research, RESEARCH_ROOT, write_report
        def progress(s):
            with LOCK: STATE["progress"] = s
            print(s, flush=True)
        study, MARKET = run_research(progress=progress)
        progress("独立重建研究账本并验收")
        folder=RESEARCH_ROOT/study["study_id"]
        audit=subprocess.run([sys.executable,str(ROOT/"tests"/"verify_research.py"),str(folder)],
                             capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=900)
        (folder/"validation.log").write_text(audit.stdout+audit.stderr,encoding="utf-8")
        audit_error=None
        if audit.returncode==0:
            from finalize_research import finalize
            finalize(folder)
            from repair_exit import run_repair, finalize_repair
            progress("运行20/40日分批退出与容量压力补充")
            run_repair(folder,progress=progress)
            progress("独立验收分批退出账本")
            repair_audit=subprocess.run([sys.executable,str(ROOT/"tests"/"verify_research.py"),str(folder),"--repair"],
                                       capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=900)
            (folder/"feasibility_repair"/"validation.log").write_text(repair_audit.stdout+repair_audit.stderr,encoding="utf-8")
            if repair_audit.returncode==0:
                finalize_repair(folder)
            else:
                audit_error="分批退出补充验收未通过，保留原结果及失败证据，未标记补充已验证"
        else:
            audit_error="独立账本验收未通过，请查看validation.json，结果未标记已验证"
            if (folder/"validation.json").exists():
                study["validation"]=json.loads((folder/"validation.json").read_text(encoding="utf-8"))
                save_json(folder/"result.json",study)
                write_report(folder,study)
        current = latest()
        if current:
            make_snapshot(current, complete=True)
            error = build_dashboard()
            if error: raise RuntimeError(error)
        with LOCK:
            STATE["progress"] = "研究完成 " + study["study_id"]
            STATE["error"] = audit_error or (None if not study["failures"] else "部分研究实验失败，完整队列结果已保留")
    except Exception as exc:
        with LOCK: STATE["error"] = str(exc)
        print(traceback.format_exc(), flush=True)
    finally:
        with LOCK: STATE["busy"] = False

def run_job(config, strategy=None):
    global MARKET
    try:
        def progress(s):
            with LOCK: STATE["progress"] = s
            print(s, flush=True)
        result, MARKET = run_suite(config, MARKET, progress=progress, dashboard_complete=True, build=True,custom_strategy=strategy)
        with LOCK:
            STATE["latest_run"] = result["run_id"]
            STATE["error"] = (None if result["status"] == "complete" else "部分实验失败，请查看结果failures") or result.get("dashboard_build_error")
    except Exception as e:
        with LOCK: STATE["error"] = str(e)
        print(traceback.format_exc(), flush=True)
    finally:
        with LOCK: STATE["busy"] = False

def activate_job(run_id):
    try:
        result=json.loads((OUTPUTS/run_id/"result.json").read_text(encoding="utf-8"))
        make_snapshot(result,complete=True)
        error=build_dashboard()
        if error: raise RuntimeError(error)
        save_json(OUTPUTS/"latest.json",{"run_id":run_id,"result":run_id+"/result.json"})
        with LOCK: STATE.update({"latest_run":run_id,"progress":"历史结果已加载","error":None})
    except Exception as e:
        with LOCK: STATE["error"]=str(e)
    finally:
        with LOCK: STATE["busy"]=False

class Handler(BaseHTTPRequestHandler):
    server_version = "DailyFactorLab/0.2"

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
        if download and target.suffix.lower() not in {".csv", ".json", ".md", ".png", ".zip"}:
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
        if path.path == "/api/blocks":
            try: return self.payload(200,catalog_response())
            except Exception as e: return self.payload(500,{"error":str(e)})
        if path.path == "/api/ai/status":
            from ai_strategy import ai_status
            return self.payload(200,ai_status())
        if path.path == "/api/strategies":
            return self.payload(200,{"strategies":[{"id":r["id"],"name":r["name"],"strategy":r["document"]} for r in read_documents(STRATEGIES)]})
        if path.path.startswith("/api/strategies/"):
            key=path.path.removeprefix("/api/strategies/")
            if not SAFE_ID.fullmatch(key): return self.payload(400,{"error":"策略标识无效"})
            p=STRATEGIES/(key+".json")
            if not p.is_file(): return self.payload(404,{"error":"策略不存在"})
            return self.payload(200,{"id":key,"strategy":json.loads(p.read_text(encoding="utf-8"))})
        if path.path == "/api/runs": return self.payload(200,{"runs":available_runs()})
        if path.path == "/api/research":
            from research import latest_research
            return self.payload(200,latest_research())
        if path.path == "/api/research/download":
            from research import RESEARCH_ROOT
            relative = parse_qs(path.query).get("path",[""])[0]
            return self.safe_file(RESEARCH_ROOT, relative, True)
        if path.path == "/api/download":
            relative = parse_qs(path.query).get("path",[""])[0]
            return self.safe_file(OUTPUTS,relative,True)
        if path.path.startswith("/api/"): return self.payload(404,{"error":"接口不存在"})
        return self.safe_file(ROOT / "dashboard" / "dist", path.path.lstrip("/") or "index.html")

    def do_POST(self):
        route=urlparse(self.path).path
        if route not in {"/api/run","/api/strategy/validate","/api/strategies","/api/block-packs","/api/runs/activate","/api/ai/confirm","/api/research/run"}:
            return self.payload(404,{"error":"接口不存在"})
        origin = self.headers.get("Origin")
        allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if origin and origin not in allowed: return self.payload(403,{"error":"只允许同源本地请求"})
        if not self.headers.get("Content-Type","").startswith("application/json"):
            return self.payload(415,{"error":"须提交JSON参数"})
        try:
            length = int(self.headers.get("Content-Length","0"))
            if not 0 < length <= 262144: raise ValueError("请求体大小不合法（最大256 KiB）")
            obj = json.loads(self.rfile.read(length),parse_constant=lambda x: (_ for _ in ()).throw(ValueError("非有限JSON数字不合法")))
            if not isinstance(obj,dict): raise ValueError("请求须为JSON对象")
            if route == "/api/research/run":
                if set(obj) - {"confirmed_full_study"} or obj.get("confirmed_full_study") is not True:
                    raise ValueError("完整研究请明确提交confirmed_full_study=true")
                with LOCK:
                    if STATE["busy"]: return self.payload(409,{"error":"已有任务正在运行"})
                    STATE.update({"busy":True,"progress":"准备120候选研究","error":None})
                threading.Thread(target=research_job,daemon=True).start()
                return self.payload(202,{"accepted":True,"scope":"2024选型、2025评价及2026年9月作业延伸"})
            if route == "/api/ai/confirm":
                from ai_strategy import confirm_strategy
                config=Config.from_dict(obj.get("config",{}))
                if not AI_LOCK.acquire(blocking=False): return self.payload(429,{"error":"已有AI确认请求正在处理"})
                try:
                    return self.payload(200,confirm_strategy(obj.get("strategy"),question=obj.get("question",""),config=config.__dict__))
                finally: AI_LOCK.release()
            if route in {"/api/strategy/validate","/api/strategies"}:
                from strategy_blocks import validate_strategy
                validation=validate_strategy(obj.get("strategy",obj))
                if route == "/api/strategy/validate": return self.payload(200,{"valid":True,**validation})
                key="strategy_"+uuid.uuid4().hex
                STRATEGIES.mkdir(exist_ok=True)
                save_json(STRATEGIES/(key+".json"),validation["strategy"])
                return self.payload(201,{"id":key,"name":validation["strategy"]["name"],"strategy":validation["strategy"]})
            if route == "/api/block-packs":
                from strategy_blocks import validate_block_pack,get_catalog
                validation=validate_block_pack(obj.get("pack",obj))
                pack=validation["pack"]
                current=read_documents(PACKS)
                others=[r["document"] for r in current if r["document"].get("id")!=pack["id"]]
                merged=get_catalog(packs=others+[pack])
                # Re-importing an identical id replaces that document, never a built-in operation.
                same=next((r for r in current if r["document"].get("id")==pack["id"]),None)
                key=same["id"] if same else "pack_"+uuid.uuid4().hex
                PACKS.mkdir(exist_ok=True)
                save_json(PACKS/(key+".json"),pack)
                return self.payload(201,{"id":key,"pack":pack,"catalog":merged,"blocks":merged["blocks"]})
            if route == "/api/runs/activate":
                run_id=obj.get("run_id","")
                if not isinstance(run_id,str) or not SAFE_ID.fullmatch(run_id) or not (OUTPUTS/run_id/"result.json").is_file():
                    raise ValueError("历史运行不存在")
                with LOCK:
                    if STATE["busy"]: return self.payload(409,{"error":"已有任务正在运行"})
                    STATE.update({"busy":True,"progress":"加载历史结果","error":None})
                threading.Thread(target=activate_job,args=(run_id,),daemon=True).start()
                return self.payload(202,{"accepted":True,"run_id":run_id})
            strategy=obj.get("strategy")
            values = obj.get("config",{k:v for k,v in obj.items() if k!="strategy"})
            if not isinstance(values,dict): raise ValueError("参数须为JSON对象")
            cfg = Config.from_dict(values)
            if strategy is not None:
                from strategy_blocks import validate_strategy
                validation=validate_strategy(strategy)
                strategy=validation["strategy"]
                selected={k:strategy.get("selection",{})[k] for k in ["top_n","rebalance"] if k in strategy.get("selection",{})}
                cfg=replace(cfg,**selected).validate()
        except Exception as e: return self.payload(400,{"error":str(e)})
        with LOCK:
            if STATE["busy"]: return self.payload(409,{"error":"已有回测正在运行"})
            STATE.update({"busy":True,"progress":"准备回测","error":None})
        threading.Thread(target=run_job,args=(cfg,strategy),daemon=True).start()
        return self.payload(202,{"accepted":True,"config":cfg.__dict__,"custom_strategy":strategy})

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8776)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    print(f"Local daily research: http://127.0.0.1:{args.port}/ (Asia/Shanghai)",flush=True)
    server.serve_forever()
