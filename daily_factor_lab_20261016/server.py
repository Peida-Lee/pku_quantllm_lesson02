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
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from lab import ROOT, Config
from run_experiments import OUTPUTS, run_suite, make_snapshot, build_dashboard, save_json

STATE = {"busy": False, "progress": "就绪", "error": None, "latest_run": None,
         "job_id": None, "job_kind": None, "status": "idle"}
LOCK = threading.Lock()
AI_LOCK = threading.Lock()
PACK_LOCK = threading.Lock()
LOCAL_JOBS = set()
JOB_THREAD = threading.Thread
JOB_FILE = OUTPUTS / "server_job.json"
MARKET = None
STRATEGIES = ROOT / "strategies"
PACKS = ROOT / "block_packs"
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


def _stamp():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def _persist_state():
    from research import save_json as atomic_save
    atomic_save(JOB_FILE,dict(STATE))


def _start_state(kind, message):
    STATE.update(busy=True,progress=message,error=None,job_id=kind+"_"+uuid.uuid4().hex,
                 job_kind=kind,status="running",started_at=_stamp(),updated_at=_stamp(),completed_at=None,
                 study_id=None,source_study_id=None,experiment_id=None,snapshot_built=False)
    LOCAL_JOBS.add(STATE["job_id"])
    _persist_state()
    return {"job_id":STATE["job_id"],"job_kind":kind}


def _finish_state():
    LOCAL_JOBS.discard(STATE.get("job_id"))
    STATE.update(busy=False,status="failed" if STATE.get("error") else "complete",updated_at=_stamp(),completed_at=_stamp())
    _persist_state()


def _launch_job(target, args=()):
    try:
        JOB_THREAD(target=target,args=args,daemon=True).start()
    except Exception as exc:
        with LOCK:
            STATE["error"]="后台线程未启动："+str(exc)
            _finish_state()
        raise


def _busy():
    state, _ = _state_snapshot()
    return state["busy"]


def _conflict(message):
    state, _ = _state_snapshot()
    return {"error":message, **{k:state.get(k) for k in ("busy","job_id","job_kind","study_id","status")}}


def _progress_matches(state, progress, allow_unbound=False):
    if state.get("study_id"):
        return state["study_id"] == progress.get("job_id")
    if not allow_unbound:
        return False
    try:
        return bool(progress.get("job_id") and state.get("started_at") and progress.get("started_at")
                    and datetime.fromisoformat(progress["started_at"]) >= datetime.fromisoformat(state["started_at"]))
    except (ValueError, TypeError):
        return False


def _state_snapshot():
    """Called under LOCK; reconcile only server state, never modify a CLI queue.

    An API job retains its accepted ID while its local thread exists. A CLI
    study has its own ID and must never borrow an older API job's identity.
    """
    from research_v4 import active_v4_running, progress_v4
    active = active_v4_running()
    p = progress_v4() if active or STATE.get("job_kind") == "research_v4" else {}
    local = STATE.get("job_id") in LOCAL_JOBS
    if STATE.get("busy") and not local and not active:
        if STATE.get("job_kind") == "research_v4" and _progress_matches(STATE, p) and p.get("status") in {"complete", "complete_with_issues", "interrupted"}:
            STATE.update(busy=False,status=p["status"],progress=p.get("phase"),error=p.get("error"),
                         study_id=p.get("job_id"),updated_at=_stamp(),completed_at=p.get("updated_at"))
        else:
            STATE.update(busy=False,status="interrupted",error="任务进程已结束或服务已重启；未标记完成",updated_at=_stamp())
        _persist_state()
    state = dict(STATE)
    if active and not local:
        current = p if p.get("status") == "running" else {}
        state.update(busy=True,job_kind="research_v4",job_id=current.get("job_id"),study_id=current.get("job_id"),
                     status="running",progress=current.get("phase", "CLI队列准备中"),error=None,
                     started_at=current.get("started_at"))
    elif local and state.get("job_kind") == "research_v4" and _progress_matches(state, p, allow_unbound=True):
        state["study_id"] = p.get("job_id")
        if STATE.get("study_id") != state["study_id"]:
            STATE["study_id"] = state["study_id"]
            _persist_state()
    return state, p


def _v4_progress_snapshot():
    state, p = _state_snapshot()
    if state.get("job_kind") != "research_v4":
        from research_v4 import progress_v4
        return progress_v4()
    if state.get("job_id") not in LOCAL_JOBS and p.get("job_id") and not _progress_matches(state, p):
        return {**p,"job_kind":"research_v4","study_id":p["job_id"]}
    if state.get("job_id") in LOCAL_JOBS and not _progress_matches(state, p):
        p = {"status":"queued", "phase":"准备冻结协议", "completed":0, "total":5616,
             "started_at":state.get("started_at"), "updated_at":state.get("updated_at")}
    elif not _progress_matches(state, p) and state.get("job_id") not in LOCAL_JOBS:
        p = {"status":state["status"], "phase":state.get("progress"), "completed":0, "total":5616}
    if state.get("status") == "building":
        p = {**p,"status":"building","phase":state.get("progress")}
    elif state.get("job_id") in LOCAL_JOBS and p.get("status") in {"complete","complete_with_issues"}:
        p = {**p,"status":"running","phase":"计算结束，等待服务核对验收与快照"}
    if state.get("error"):
        p = {**p,"status":state["status"],"error":state["error"]}
    return {**p,"job_kind":"research_v4","study_id":state.get("study_id") or p.get("job_id"),"job_id":state.get("job_id")}


def _api_config(values):
    if not isinstance(values, dict):
        raise ValueError("参数须为JSON对象")
    cfg = Config.from_dict(values)
    if cfg.execution_model == "legacy" and (cfg.wind_down_days or cfg.liquidate_end):
        raise ValueError("退出/清算参数须使用daily_conservative，legacy不支持")
    if cfg.st_policy == "strict":
        raise ValueError("原包无真实历史ST状态，strict政策不可运行")
    return cfg


def _checked_v4_study():
    from research_v4 import latest_v4, code_hashes, source_identity
    study = latest_v4()
    if not study.get("snapshot_ready") or study.get("validation", {}).get("passed") is not True:
        raise ValueError("V4研究尚未通过独立验收，不得激活或生成已审核快照")
    if study.get("protocol", {}).get("pilot"):
        raise ValueError("试跑研究不能作为正式V4研究激活")
    if study.get("protocol", {}).get("code_sha256") != code_hashes():
        raise ValueError("当前计算代码与该V4冻结版本不同，不能声称复现；请使用对应冻结版本")
    if study.get("protocol", {}).get("source_identity") != source_identity():
        raise ValueError("当前原始ZIP与该V4冻结源身份不同，拒绝混合数据或误称复现")
    return study


def _restore_state():
    if JOB_FILE.exists():
        try:
            loaded=json.loads(JOB_FILE.read_text(encoding="utf-8"))
            if not isinstance(loaded,dict): raise ValueError("旧任务状态须为对象")
            STATE.update(loaded)
            with LOCK: _state_snapshot()
        except (OSError,ValueError,TypeError):
            STATE.update(busy=False,status="interrupted",error="旧任务状态不可读，未标记完成")


def v4_job(workers=6,resume=None,retry_failed=False):
    try:
        command=[sys.executable,str(ROOT/"research_v4.py"),"--workers",str(workers)]
        if resume: command.extend(["--resume",str(resume)])
        if retry_failed: command.append("--retry-failed")
        OUTPUTS.mkdir(exist_ok=True)
        log=OUTPUTS/(STATE["job_id"]+".log")
        with log.open("w",encoding="utf-8") as stream:
            result=subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if result.returncode: raise RuntimeError("V4队列中断，检查"+log.name+"及progress.json；成功检查点保留")
        from research_v4 import progress_v4
        p=progress_v4(); study=_checked_v4_study()
        if not p.get("snapshot_ready") or study.get("study_id")!=p.get("job_id") or not _progress_matches(STATE,p,allow_unbound=True):
            raise RuntimeError("V4数值验收未通过，旧快照保留，检查本次validation.json")
        with LOCK:
            STATE["study_id"]=study["study_id"]
        current=latest()
        if current:
            with LOCK:
                STATE.update(status="building",progress="V4数值已验收，生成快照",updated_at=_stamp())
                _persist_state()
            make_snapshot(current,complete=True)
            error=build_dashboard()
            if error: raise RuntimeError(error)
            with LOCK: STATE["snapshot_built"]=True
        with LOCK:
            STATE["progress"]=("V4完成并生成已审核快照 " if current else "V4数值验收完成；暂无普通回测，未生成仪表盘快照 ")+study["study_id"]
    except Exception as exc:
        with LOCK: STATE["error"]=str(exc)
        print(traceback.format_exc(),flush=True)
    finally:
        with LOCK: _finish_state()

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
        with LOCK: _finish_state()

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
            STATE["snapshot_built"] = "dashboard_build_error" in result and result["dashboard_build_error"] is None
    except Exception as e:
        with LOCK: STATE.update(error=str(e),snapshot_built=False)
        print(traceback.format_exc(), flush=True)
    finally:
        with LOCK: _finish_state()

def activate_job(run_id):
    try:
        result=json.loads((OUTPUTS/run_id/"result.json").read_text(encoding="utf-8"))
        make_snapshot(result,complete=True)
        error=build_dashboard()
        if error: raise RuntimeError(error)
        save_json(OUTPUTS/"latest.json",{"run_id":run_id,"result":run_id+"/result.json"})
        with LOCK: STATE.update({"latest_run":run_id,"progress":"历史结果已加载","error":None,"snapshot_built":True})
    except Exception as e:
        with LOCK: STATE.update(error=str(e),snapshot_built=False)
    finally:
        with LOCK: _finish_state()

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
            with LOCK: state, _ = _state_snapshot()
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
        if path.path == "/api/research/v4":
            from research_v4 import latest_v4
            return self.payload(200,latest_v4())
        if path.path == "/api/research/v4/catalog":
            from v4_factors import catalog
            return self.payload(200,{"factors":catalog()})
        if path.path == "/api/research/v4/progress":
            with LOCK:
                p=_v4_progress_snapshot()
            return self.payload(200,p)
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
        if route not in {"/api/run","/api/strategy/validate","/api/strategies","/api/block-packs","/api/runs/activate","/api/ai/confirm","/api/research/run","/api/research/v4/run","/api/research/v4/activate"}:
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
            if route == "/api/research/v4/run":
                from research_v4 import OUTPUT
                if set(obj)-{"confirmed_full_study","workers","resume_study","retry_failed"} or obj.get("confirmed_full_study") is not True:
                    raise ValueError("须提交confirmed_full_study=true")
                workers=obj.get("workers",6)
                if type(workers) is not int or not 1<=workers<=6: raise ValueError("workers须为1至6整数")
                retry=obj.get("retry_failed",False)
                if type(retry) is not bool: raise ValueError("retry_failed须为布尔值")
                resume=obj.get("resume_study")
                if resume is not None:
                    if not isinstance(resume,str) or not SAFE_ID.fullmatch(resume) or not (OUTPUT/resume/"protocol.json").is_file(): raise ValueError("恢复study不存在")
                    resume=OUTPUT/resume
                with LOCK:
                    if _busy(): return self.payload(409,_conflict("已有任务正在运行"))
                    identity=_start_state("research_v4","准备V4冻结网格，成功检查点可恢复")
                _launch_job(v4_job,(workers,resume,retry))
                return self.payload(202,{"accepted":True,**identity})
            if route == "/api/research/v4/activate":
                from research_v4 import strategy_document,config_for
                if set(obj)!={"experiment_id"}: raise ValueError("仅接受experiment_id")
                if not isinstance(obj["experiment_id"],str): raise ValueError("experiment_id须为字符串")
                study=_checked_v4_study()
                candidate=next((c for c in study.get("candidates",[]) if c["experiment_id"]==obj["experiment_id"]),None)
                if candidate is None: raise ValueError("候选不存在于已审核V4研究")
                if candidate.get("checks",{}).get("numeric_pass") is not True:
                    raise ValueError("该候选未通过独立数值验收，不能作为已审核候选激活")
                strategy=strategy_document(candidate)
                cfg=config_for(candidate,"evaluation_2025",protocol=study["protocol"])
                with LOCK:
                    if _busy(): return self.payload(409,_conflict("已有任务正在运行"))
                    identity=_start_state("run","准备V4候选独立回测")
                    STATE.update(source_study_id=study["study_id"],experiment_id=candidate["experiment_id"])
                    _persist_state()
                _launch_job(run_job,(cfg,strategy))
                return self.payload(202,{"accepted":True,**identity,"source_study_id":study["study_id"],"experiment_id":candidate["experiment_id"],"config":cfg.__dict__})
            if route == "/api/research/run":
                if set(obj) - {"confirmed_full_study"} or obj.get("confirmed_full_study") is not True:
                    raise ValueError("完整研究请明确提交confirmed_full_study=true")
                with LOCK:
                    if _busy(): return self.payload(409,_conflict("已有任务正在运行"))
                    identity=_start_state("research_v3","准备既有V3冻结研究")
                _launch_job(research_job)
                return self.payload(202,{"accepted":True,**identity,"scope":"2024选型、2025评价及2026年9月作业延伸"})
            if route == "/api/ai/confirm":
                from ai_strategy import confirm_strategy
                if set(obj)-{"config","strategy","question"}: raise ValueError("未知AI确认字段")
                config=_api_config(obj.get("config",{}))
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
                with PACK_LOCK:
                    current=read_documents(PACKS)
                    others=[r["document"] for r in current if r["document"].get("id")!=pack["id"]]
                    merged=get_catalog(packs=others+[pack])
                    same=next((r for r in current if r["document"].get("id")==pack["id"]),None)
                    key=same["id"] if same else "pack_"+uuid.uuid4().hex
                    PACKS.mkdir(exist_ok=True)
                    from research import save_json as atomic_save
                    atomic_save(PACKS/(key+".json"),pack)
                return self.payload(201,{"id":key,"pack":pack,"catalog":merged,"blocks":merged["blocks"]})
            if route == "/api/runs/activate":
                if set(obj)!={"run_id"}: raise ValueError("仅接受run_id")
                run_id=obj.get("run_id","")
                if not isinstance(run_id,str) or not SAFE_ID.fullmatch(run_id) or not (OUTPUTS/run_id/"result.json").is_file():
                    raise ValueError("历史运行不存在")
                with LOCK:
                    if _busy(): return self.payload(409,_conflict("已有任务正在运行"))
                    identity=_start_state("activate","加载历史结果")
                _launch_job(activate_job,(run_id,))
                return self.payload(202,{"accepted":True,**identity,"run_id":run_id})
            strategy=obj.get("strategy")
            if "config" in obj and set(obj)-{"config","strategy"}:
                raise ValueError("嵌套请求外层仅接受config/strategy")
            values = obj.get("config",{k:v for k,v in obj.items() if k!="strategy"})
            if not isinstance(values,dict): raise ValueError("参数须为JSON对象")
            cfg = _api_config(values)
            if strategy is not None:
                from strategy_blocks import validate_strategy
                validation=validate_strategy(strategy)
                strategy=validation["strategy"]
                selected={k:strategy.get("selection",{})[k] for k in ["top_n","rebalance"] if k in strategy.get("selection",{})}
                cfg=replace(cfg,**selected).validate()
        except Exception as e: return self.payload(400,{"error":str(e)})
        with LOCK:
            if _busy(): return self.payload(409,_conflict("已有回测正在运行"))
            identity=_start_state("run","准备回测")
        try:
            _launch_job(run_job,(cfg,strategy))
        except Exception as exc:
            return self.payload(500,{"error":str(exc),"job_id":STATE.get("job_id"),"status":"failed"})
        return self.payload(202,{"accepted":True,**identity,"config":cfg.__dict__,"custom_strategy":strategy})

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8776)
    args = parser.parse_args()
    from research import reserve_performance_cores
    reserve_performance_cores()
    _restore_state()
    server = ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    print(f"Local daily research: http://127.0.0.1:{args.port}/ (Asia/Shanghai)",flush=True)
    server.serve_forever()
