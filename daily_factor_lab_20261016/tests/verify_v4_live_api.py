"""Read/validate the real loopback service; never submit a runnable job."""
import json
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
BASE="http://127.0.0.1:8776"


def request(path,body=None,origin=None,raw=None):
    payload=raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers={"Content-Type":"application/json"} if payload is not None else {}
    if origin:headers["Origin"]=origin
    req=urllib.request.Request(BASE+path,data=payload,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=15) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as r:return r.code,json.load(r)


def main():
    checks=[]
    def check(name,passed,**detail):checks.append({"name":name,"passed":bool(passed),**detail})
    code,cat=request("/api/research/v4/catalog")
    check("real_v4_catalog_78",code==200 and len(cat.get("factors",[]))==78)
    code,blocks=request("/api/blocks")
    ids={b["id"] for b in blocks.get("blocks",[])}
    check("real_native_library_78",code==200 and sum(i.startswith("v4_") for i in ids)==78)
    doc={"format":"daily-factor-strategy","version":1,"name":"V4 live static check",
         "score":{"op":"v4_reversal_w005"},"selection":{}}
    code,result=request("/api/strategy/validate",{"strategy":doc})
    check("real_static_validation_retains_inherited_selection",code==200 and result.get("valid") is True and "top_n" not in result["strategy"]["selection"])
    bad=json.loads(json.dumps(doc));bad["score"]["op"]="v4_unknown"
    code,_=request("/api/strategy/validate",{"strategy":bad})
    check("unknown_library_entry_rejected",code==400)
    # Every POST below is invalid before any thread/job may be created.
    for name,path,body in (
        ("bool_workers","/api/research/v4/run",{"confirmed_full_study":True,"workers":True}),
        ("unknown_queue_field","/api/research/v4/run",{"confirmed_full_study":True,"surprise":1}),
        ("negative_exit","/api/run",{"config":{"wind_down_days":-1}}),
        ("legacy_exit","/api/run",{"config":{"execution_model":"legacy","wind_down_days":20}}),
        ("unknown_nested_field","/api/run",{"config":{},"surprise":1}),
        ("unavailable_true_ST","/api/run",{"config":{"st_policy":"strict"}})):
        code,_=request(path,body);check("real_reject_"+name,code==400,http_status=code)
    code,_=request("/api/strategy/validate",{"strategy":doc},origin="https://example.invalid")
    check("cross_origin_post_rejected",code==403)
    code,_=request("/api/strategy/validate",raw=b'{"strategy":NaN}')
    check("nonfinite_JSON_rejected",code==400)
    code,ai=request("/api/ai/status")
    check("AI_status_read_only_and_no_credentials",code==200 and ai.get("read_only") is True and not any(k in ai for k in ("key","api_key","endpoint")))
    if ai.get("configured") is False:
        code,result=request("/api/ai/confirm",{"strategy":doc,"config":{"start":"2025-01-02","end":"2025-12-31","execution_model":"daily_conservative","st_policy":"price_proxy","wind_down_days":20}})
        context=result.get("ai_context",{})
        check("real_AI_local_only_with_V4_constraints",code==200 and result.get("used_ai") is False and context.get("execution_config",{}).get("wind_down_days")==20 and len(context.get("factor_definitions",[]))==1)
    result={"created_at":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),"passed":all(c["passed"] for c in checks),
            "scope":"Real 127.0.0.1 API GET/static validation/invalid requests only; no runnable job, actual AI model, saved strategy or live queue monitoring submitted",
            "checks":checks}
    (ROOT/"metadata"/"V4_LIVE_API_VALIDATION.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"passed":result["passed"],"checks":len(checks),"failures":[c for c in checks if not c["passed"]]},ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__=="__main__":main()
