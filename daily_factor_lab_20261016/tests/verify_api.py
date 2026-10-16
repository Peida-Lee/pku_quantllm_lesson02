"""Exercise a running loopback server, including one real short custom rerun.

This intentionally leaves the custom run as latest. Restore your preferred run
afterward with outputs/latest.json and run_experiments.make_snapshot().
"""
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = "http://127.0.0.1:8776"
ROOT = Path(__file__).resolve().parents[1]
checks = []

def request(path, payload=None, origin=None, content_type="application/json"):
    req = Request(BASE+path, data=json.dumps(payload).encode() if payload is not None else None,
                  headers={"Content-Type":content_type, **({"Origin":origin} if origin else {})})
    try:
        with urlopen(req,timeout=20) as r:
            return r.status, r.read(), r.headers.get("Content-Type","")
    except HTTPError as e: return e.code,e.read(),e.headers.get("Content-Type","")

def check(label, condition):
    checks.append({"check":label,"passed":bool(condition)})
    if not condition: raise AssertionError(label)

status, body, _ = request("/api/status")
check("status_endpoint",status==200 and not json.loads(body)["busy"])
status, body, content_type = request("/")
check("compiled_html",status==200 and "text/html" in content_type and len(body)>10000)
status,body,_ = request("/api/result")
before = json.loads(body)
check("full_initial_run",status==200 and len(before["summary"])==15 and len(before["ic"])==5)
status,_,_ = request("/api/download?path=../config.json")
check("download_path_escape_blocked",status==404)
status,body,_ = request("/api/download?path="+before["files"]["summary"])
check("summary_download",status==200 and b"annual_return" in body)
status,_,_ = request("/api/run",{"top_n":0})
check("invalid_params_rejected",status==400)
status,_,_ = request("/api/run",{"top_n":100},origin="https://example.com")
check("cross_origin_post_rejected",status==403)
status,_,_ = request("/api/run",{"top_n":100},content_type="text/plain")
check("non_json_post_rejected",status==415)
cfg = {**before["config"],"start":"2021-01-01","end":"2021-02-15","top_n":10,"rebalance":"monthly"}
status,_,_ = request("/api/run",{"config":cfg},origin=BASE)
check("real_rerun_accepted",status==202)
status,_,_ = request("/api/run",{"config":cfg},origin=BASE)
check("simultaneous_job_rejected",status==409)
deadline = time.monotonic()+150
while True:
    _,body,_=request("/api/status")
    state=json.loads(body)
    if not state["busy"]: break
    if time.monotonic()>deadline: raise TimeoutError("real rerun did not finish")
    time.sleep(1)
check("real_rerun_and_dashboard_build_completed",state["error"] is None)
_,body,_=request("/api/result")
after=json.loads(body)
check("new_run_identity",after["run_id"]!=before["run_id"])
check("requested_config_applied",after["config"]==cfg)
check("all_scenarios_and_ic_complete",len(after["summary"])==15 and len(after["ic"])==5 and not after["failures"])
check("bounded_custom_interval",all(r["date"]<="2021-02-15" and r["date"]>="2021-01-01" for r in after["curves"]))
snapshot=json.loads((ROOT/"dashboard"/"src"/"data.json").read_text(encoding="utf-8"))
check("snapshot_tracks_new_run",snapshot["run_id"]==after["run_id"] and snapshot["config"]==cfg)
report={"checks":checks,"passed":len(checks),"before_run":before["run_id"],"custom_run":after["run_id"],"restoration_required":True}
(ROOT/"api_validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False))
