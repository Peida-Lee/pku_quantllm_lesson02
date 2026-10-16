"""Versioned, resumable ZIP-only factor exploration; all candidates share stress tests.

2025 was already observed.  This is retrospective research, not independent
confirmation.  Neither a zero book position nor numerical agreement establishes
historical ST truth or opening-auction execution.
"""
import os
for _key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_key] = "1"

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time
import traceback
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from lab import ROOT, SOURCE, Config, Market, simulate, rebalance_days
from research import UNIVERSES, PERIODS, LIMITATIONS, base_config, finite_json, save_json, now, reserve_performance_cores
from v4_factors import FACTOR_SPECS, FactorEngine, candidate_grid, catalog

OUTPUT = ROOT / "research_outputs"
LATEST = OUTPUT / "latest_v4.json"
ACTIVE = OUTPUT / "active_v4.json"
LOCK_PATH = OUTPUT / "research_v4.lock"
CODE_FILES = ("lab.py", "execution_metadata.py", "research.py", "strategy_blocks.py", "v4_factors.py", "research_v4.py")
MONEY_TOL = .001
SCENARIOS = ("base", "stress", "capacity")
_WORKER = {}


def code_hashes():
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in CODE_FILES}


def source_identity():
    source=Path(SOURCE).resolve()
    if source != (ROOT.parent/"feathers.zip").resolve():
        raise ValueError("V4仅允许用户指定D:/quant/feathers.zip；拒绝环境变量替换源")
    stat=source.stat()
    with source.open("rb") as stream:
        digest=hashlib.file_digest(stream,"sha256").hexdigest()
    return {"path":str(source),"size_bytes":stat.st_size,"sha256":digest}


def grid():
    return [{**r, "factor_id": r["factor"], "family": FACTOR_SPECS[r["factor"]]["family"],
             "idea_id": FACTOR_SPECS[r["factor"]]["idea_id"], "window": FACTOR_SPECS[r["factor"]]["window"],
             "formula": FACTOR_SPECS[r["factor"]]["formula"], "required_history": FACTOR_SPECS[r["factor"]]["required_history_rows"],
             "required_fields": FACTOR_SPECS[r["factor"]]["required_fields"], "boundaries": FACTOR_SPECS[r["factor"]]["boundaries"],
             "universe_name": UNIVERSES[r["universe"]]["name"]}
            for r in candidate_grid()]


def config_for(candidate, role, scenario="base", protocol=None):
    spec = FACTOR_SPECS[candidate["factor_id"]]
    start, end = (protocol or {}).get("periods",PERIODS)[role]
    pool = (protocol or {}).get("universes",UNIVERSES)[candidate["universe"]]
    values = dict((protocol or {}).get("base_config", asdict(base_config(wind_down_days=20))))
    values.update(start=start, end=end, top_n=candidate["top_n"], rebalance=candidate["rebalance"],
                  min_history=max(120, spec["required_history_rows"]), universe=pool["universe"],
                  liquidity_top_n=pool["liquidity_top_n"])
    if scenario == "capacity":
        values.update(max_participation=.001, max_daily_participation=.001)
    elif scenario not in ("base", "stress"):
        raise ValueError("Unknown V4 scenario")
    return Config.from_dict(values)


def strategy_document(candidate):
    """All library entries are native, exportable strategy-block documents."""
    return {"format": "daily-factor-strategy", "version": 1,
            "name": candidate["name"] + " / " + candidate["universe_name"],
            "score": {"op": "v4_" + candidate["factor_id"], "args": [], "params": {}},
            "selection": {"top_n": candidate["top_n"], "rebalance": candidate["rebalance"], "direction": "desc"},
            "packs": []}


def _quarter_returns(daily, capital):
    previous = np.r_[capital, daily.nav.to_numpy()[:-1]]
    result = {}
    for label, idx in daily.groupby(pd.to_datetime(daily.date).dt.to_period("Q")).groups.items():
        positions = np.asarray(list(idx), dtype=int)
        result[str(label)] = float(daily.nav.iloc[positions[-1]] / previous[positions[0]] - 1)
    halves = {}
    for label, mask in (("H1", daily.date.str[5:7] <= "06"), ("H2", daily.date.str[5:7] >= "07")):
        ids = np.flatnonzero(mask)
        if len(ids): halves[label] = float(daily.nav.iloc[ids[-1]] / previous[ids[0]] - 1)
    return result, halves


def signal_signature(m, score, cfg):
    """Training-period planned selections only; no future returns in equivalence."""
    ix = np.flatnonzero((m.dates >= cfg.start) & (m.dates <= cfg.end) & (np.arange(len(m.dates)) > 0))
    schedule = sorted(rebalance_days(m.dates, ix, cfg.rebalance))
    end = ix[-min(cfg.wind_down_days, len(ix))] if cfg.wind_down_days else len(m.dates)
    eligible = m.eligible(cfg)
    digest = hashlib.sha256()
    counts, uniqueness, coverage = [], [], []
    for i in schedule:
        if i >= end: continue
        valid = eligible[i-1] & np.isfinite(score[i-1])
        ids = np.flatnonzero(valid)
        selected = ids[np.lexsort((m.symbols[ids], -score[i-1, ids]))][:cfg.top_n]
        digest.update(str(m.dates[i].date()).encode())
        digest.update("|".join(sorted(map(str, m.symbols[selected]))).encode())
        counts.append(len(ids))
        uniqueness.append(len(np.unique(score[i-1, ids])) / len(ids) if len(ids) else 0.)
        coverage.append(len(ids) / max(1, np.count_nonzero(eligible[i-1])))
    return {"training_signal_signature": digest.hexdigest(),
            "signature_definition":"2024 equal-weight planned baskets, sorted symbols; excludes buy priority and does not establish identical fills",
            "signal_rebalances": len(counts),
            "min_valid_names": min(counts, default=0), "mean_valid_names": float(np.mean(counts)) if counts else 0.,
            "mean_unique_score_fraction": float(np.mean(uniqueness)) if counts else 0.,
            "mean_signal_coverage": float(np.mean(coverage)) if counts else 0.}


def _record_path(out, candidate, role, scenario, detailed=False):
    return out / ("detailed" if detailed else "runs") / candidate["experiment_id"] / role / scenario


def _worker_init(out, protocol):
    reserve_performance_cores()
    import pyarrow as pa
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    if code_hashes() != protocol["code_sha256"]:
        raise RuntimeError("Compute code changed after protocol freeze")
    m = Market(protocol["source_identity"]["path"])
    _WORKER.update(market=m, engine=FactorEngine(m, max_cache=1), out=Path(out), protocol=protocol)


def _run_one(candidate, role, scenario, detailed=False, retry_failed=False):
    m, engine, out, protocol = (_WORKER[k] for k in ("market", "engine", "out", "protocol"))
    folder = _record_path(out, candidate, role, scenario, detailed)
    path = folder / "record.json"
    folder.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    record = {"experiment_id": candidate["experiment_id"], "factor_id": candidate["factor_id"],
              "period_role": role, "scenario": scenario, "folder": folder.relative_to(out).as_posix(),
              "detailed": detailed, "status": "failed", "started_at": now()}
    try:
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                if old.get("experiment_id")!=candidate["experiment_id"] or old.get("period_role")!=role or old.get("scenario")!=scenario:
                    raise ValueError("checkpoint identity mismatch")
                if old.get("status")=="success" and not (folder/"daily.csv").is_file():
                    raise ValueError("checkpoint daily.csv missing")
                if old.get("status")=="success" and detailed and not all((folder/name).is_file() for name in ("trades.csv","holdings.csv","orders.csv","diagnostics.json")):
                    raise ValueError("detailed checkpoint files missing")
                if old.get("status") == "success" or not retry_failed: return old
            except (ValueError, OSError) as exc:
                preserved=path.with_name("record.corrupt_"+str(time.time_ns())+".json")
                path.replace(preserved)
                if not retry_failed: raise ValueError("损坏检查点已保留；需显式retry_failed重算该项："+str(exc))
        cfg = config_for(candidate, role, scenario, protocol)
        score = engine.compute(candidate["factor_id"], m.eligible(cfg))
        multiplier = 2. if scenario == "stress" else 1.
        r = simulate(m, score, cfg, cost_multiplier=multiplier, keep_trades=detailed)
        d, metrics = r["daily"], finite_json(r["metrics"])
        expected = int(np.count_nonzero((m.dates >= cfg.start) & (m.dates <= cfg.end)))
        if len(d) != expected or not np.isfinite(d[["nav", "cash", "return"]].to_numpy()).all():
            raise AssertionError("Incomplete/nonfinite daily result")
        if d.cash.min() < -MONEY_TOL or r["execution_diagnostics"]["max_combined_capacity_excess"] > MONEY_TOL:
            raise AssertionError("Cash/capacity invariant failed")
        quarters, halves = _quarter_returns(d, cfg.capital)
        residual = float(r["terminal"]["residual_value"])
        exit_by_trades = bool(residual <= MONEY_TOL and not len(r["holdings"]) and metrics["writeoff_loss"] == 0.)
        record.update(config=asdict(cfg), cost_multiplier=multiplier,
                      metrics={**metrics, "quarter_returns": quarters, "half_returns": halves,
                               "terminal_position_value": residual, "terminal_positions": len(r["holdings"]),
                               "exit_by_trades": exit_by_trades,
                               "book_position_zero": not len(r["holdings"]),
                               "blocked_attempts": sum(r["blocked"].values()),
                               "capacity_excess": r["execution_diagnostics"]["max_combined_capacity_excess"]},
                      terminal=r["terminal"], blocked=r["blocked"], execution_diagnostics=r["execution_diagnostics"])
        if role == "selection_2024": record["signal_diagnostics"] = signal_signature(m, score, cfg)
        temporary = folder / "daily.csv.tmp"
        d.to_csv(temporary, index=False, encoding="utf-8-sig", float_format="%.17g")
        temporary.replace(folder / "daily.csv")
        if detailed:
            for key in ("trades", "holdings", "orders"):
                r[key].to_csv(folder / (key + ".csv"), index=False, encoding="utf-8-sig", float_format="%.17g")
            save_json(folder / "diagnostics.json", finite_json({k: r[k] for k in ("metrics", "terminal", "blocked", "execution_diagnostics")}))
        record["status"]="success"
    except Exception as exc:
        record.update(status="failed", error=str(exc), traceback=traceback.format_exc())
    record["elapsed_seconds"] = time.monotonic() - started
    record["completed_at"] = now()
    save_json(path, finite_json(record))
    return finite_json(record)


def _factor_group(task):
    fid, phase, candidates, retry_failed = task
    _WORKER["engine"].clear()
    output = []
    for c in candidates:
        for scenario in (("base",) if phase == "selection_2024" else SCENARIOS):
            output.append(_run_one(c, phase, scenario, retry_failed=retry_failed))
    _WORKER["engine"].clear()
    return output


def _detail_group(task):
    candidate, extension, retry_failed = task
    output = [_run_one(candidate, "evaluation_2025", s, detailed=True, retry_failed=retry_failed) for s in SCENARIOS]
    if extension: output.append(_run_one(candidate, "assignment_2025_2026sep", "base", detailed=True, retry_failed=retry_failed))
    _WORKER["engine"].clear()
    return output


def choose_training(candidates, records):
    success = {r["experiment_id"]: r for r in records if r["status"] == "success"}
    safe = [c for c in candidates if c["experiment_id"] in success and (
        success[c["experiment_id"]]["metrics"]["mean_invested"] >= .25 and
        success[c["experiment_id"]]["metrics"]["max_drawdown"] >= -.40 and
        success[c["experiment_id"]]["metrics"]["total_return"] > 0 and
        success[c["experiment_id"]]["metrics"]["exit_by_trades"])]
    safe.sort(key=lambda c: (-success[c["experiment_id"]]["metrics"]["sharpe"],
                             -success[c["experiment_id"]]["metrics"]["total_return"], c["experiment_id"]))
    shortlist = safe[:6]
    seen_ideas = {c["idea_id"] for c in shortlist}
    for c in safe:
        if len(shortlist) >= 12: break
        if c["idea_id"] not in seen_ideas:
            shortlist.append(c)
            seen_ideas.add(c["idea_id"])
    audit = [c["experiment_id"] for c in safe[:3]]
    for fid in ("reversal_w005", "lowrange_w020", "lowvol_w020"):
        matching = [c for c in candidates if c["factor_id"] == fid and c["experiment_id"] in success]
        matching.sort(key=lambda c: (-success[c["experiment_id"]]["metrics"]["sharpe"], c["experiment_id"]))
        if matching and matching[0]["experiment_id"] not in audit: audit.append(matching[0]["experiment_id"])
    signatures = defaultdict(list)
    for c in candidates:
        r = success.get(c["experiment_id"])
        if r: signatures[r["signal_diagnostics"]["training_signal_signature"]].append(c["experiment_id"])
    return {"frozen_at": now(), "selected_on": "selection_2024", "retrospective": True,
            "selected_id": safe[0]["experiment_id"] if safe else None,
            "shortlist": [c["experiment_id"] for c in shortlist], "audit_ids": audit,
            "admissible_count": len(safe), "no_fallback": True,
            "training_unique_signal_signatures": len(signatures),
            "training_equivalent_groups": [v for v in signatures.values() if len(v) > 1]}


def classify(c):
    names = ("selection_2024", "evaluation_2025", "stress_2025", "capacity_2025")
    complete = all(isinstance(c.get(k), dict) and c[k].get("status") == "success" for k in names)
    values = [c[k] for k in names if isinstance(c.get(k), dict) and c[k].get("status") == "success"]
    checks = {"all_required_runs_complete": complete,
              "numeric_pass": False,  # filled only from independent persisted-file validation
              "base_exit_by_trades": bool(c.get("evaluation_2025", {}).get("exit_by_trades", False)),
              "no_writeoff_all_scenarios": complete and all(r["writeoff_loss"] == 0 for r in values),
              "all_scenarios_exit_by_trades": complete and all(r["exit_by_trades"] for r in values),
              "positive_base_return": c.get("evaluation_2025", {}).get("total_return", -math.inf) > 0,
              "positive_stress_return": c.get("stress_2025", {}).get("total_return", -math.inf) > 0,
              "positive_capacity_return": c.get("capacity_2025", {}).get("total_return", -math.inf) > 0,
              "positive_halves": set(c.get("evaluation_2025", {}).get("half_returns", {}))=={"H1","H2"} and all(v > 0 for v in c["evaluation_2025"]["half_returns"].values()),
              "capacity_mean_invested_ge25pct": c.get("capacity_2025", {}).get("mean_invested", 0) >= .25,
              "max_drawdown_within40pct": complete and all(r["max_drawdown"] >= -.40 for r in values),
              "has_writeoff": any(r["writeoff_loss"] > 0 for r in values),
              "historical_ST_verified": False, "opening_auction_verified": False}
    c["checks"] = checks
    c["readiness"] = "待独立数值验收" if complete else "运行缺失或失败"
    return c


def assemble(out, protocol, candidates, selection, records, details):
    indexed = {(r["experiment_id"], r["period_role"], r["scenario"]): r for r in records}
    rows, failures = [], []
    for c0 in candidates:
        c = dict(c0)
        for field, role, scenario in (("selection_2024", "selection_2024", "base"),
                                      ("evaluation_2025", "evaluation_2025", "base"),
                                      ("stress_2025", "evaluation_2025", "stress"),
                                      ("capacity_2025", "evaluation_2025", "capacity")):
            r = indexed.get((c["experiment_id"], role, scenario))
            c[field] = ({"status": r["status"], **r.get("metrics", {}), "folder": r["folder"],
                         "error": r.get("error")} if r else {"status": "missing"})
            if field == "selection_2024" and r: c["signal_diagnostics"] = r.get("signal_diagnostics", {})
        rows.append(classify(c))
    failures = [{k: r[k] for k in ("experiment_id", "period_role", "scenario", "error", "folder") if k in r}
                for r in records + details if r["status"] != "success"]
    curves = []
    for r in details:
        if r["status"] != "success": continue
        d = pd.read_csv(out / r["folder"] / "daily.csv", float_precision="round_trip")
        nav = d.nav.to_numpy()
        peak = np.maximum.accumulate(np.r_[protocol["base_config"]["capital"], nav])[1:]
        curves.extend({"experiment_id": r["experiment_id"], "scenario": r["scenario"], "period_role": r["period_role"],
                       "date": date, "nav_index": float(n/protocol["base_config"]["capital"]), "drawdown": float(n/p-1)}
                      for date, n, p in zip(d.date, nav, peak))
    return {"study_id": out.name, "status": "computed_pending_validation", "snapshot_ready": False,
            "generated_at": now(), "protocol": protocol, "selection": selection,
            "candidates": rows, "curves": curves, "failures": failures, "detailed_records": details,
            "validation": {"status": "not_run"}, "limitations": LIMITATIONS + [
                f"本研究{len(candidates)}个配置由78个参数规格、26个参数族组合而来，配置数量不是独立经济idea数量。训练期等价选股签名另行计数。",
                "每个候选同跑2024基准和2025基准/加倍费用/0.1%容量；提前20交易日退出在本轮开始前固定，源于此前后验修复。",
                "稳健性标签仅为明确门槛的同源回顾性筛选，不等于未来盈利或可实盘。没有达到门槛的候选不会被强行指定为推荐。"],
            "files": {k: f"{out.name}/{v}" for k, v in {
                "report": "report.md", "experiments": "experiments.csv", "candidate_checks": "candidates.csv",
                "result": "result.json", "protocol": "protocol.json", "selection": "selection_frozen_before_2025.json",
                "validation": "validation.json", "factors": "factor_catalog.json", "curves": "curves.csv"}.items()}}


def flatten_candidates(study):
    result = []
    for c in study.get("candidates", []):
        row = dict(c)
        for field in ("selection_2024", "evaluation_2025", "stress_2025", "capacity_2025"):
            for key, value in c[field].items():
                if not isinstance(value, (dict, list)): row[field + "_" + key] = value
        result.append(row)
    return result


def latest_v4():
    if not LATEST.exists(): return {"status": "not_run", "candidates": [], "curves": [], "failures": []}
    pointer = json.loads(LATEST.read_text(encoding="utf-8"))
    path = OUTPUT / pointer["study_id"] / "result.json"
    path.resolve().relative_to(OUTPUT.resolve())
    return json.loads(path.read_text(encoding="utf-8"))


def progress_v4():
    if not ACTIVE.exists(): return {"status": "not_run", "job_kind": "research_v4", "completed": 0, "total": len(grid())*4}
    pointer = json.loads(ACTIVE.read_text(encoding="utf-8"))
    path = OUTPUT / pointer["study_id"] / "progress.json"
    path.resolve().relative_to(OUTPUT.resolve())
    if not path.exists(): return {"status":"unknown"}
    state=json.loads(path.read_text(encoding="utf-8"))
    if state.get("status")=="running" and not active_v4_running():
        state.update(status="interrupted",error="计算进程不再持锁，可用冻结协议恢复",resume_study=pointer["study_id"])
    return state


def active_v4_running():
    if not LOCK_PATH.exists(): return False
    try:
        import psutil
        lock=json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        return psutil.pid_exists(int(lock["pid"])) and abs(psutil.Process(int(lock["pid"])).create_time()-lock["process_created"])<1
    except (ValueError,OSError,KeyError): return False


def attach_snapshot(snapshot):
    study = latest_v4()
    if not study.get("snapshot_ready"): return
    source = {"label": "原ZIP的V4回顾性参数网格与逐项压力研究", "executedAt": study["generated_at"],
              "timezone": "Asia/Shanghai", "files": ["feathers.zip", *CODE_FILES, f"research_outputs/{study['study_id']}/result.json"],
              "coverage": {"selection": "2024-01-02至2024-12-31", "evaluation": "2025-01-02至2025-12-31"},
              "filters": ["2024冻结选型；2025全候选三情景；未知ST用价格风险代理"], "caveats": study["limitations"],
              "snapshot_revision": study["snapshot_revision"],
              "metricDefinitions": [{"label": "研究筛选通过", "definition": "四项已验收情景完整、无减记且均模型卖出退出、2025三情景正收益、双半年正收益、容量场景平均仓位≥25%、各情景回撤≤40%；不代表真实ST或竞价核实"}]}
    snapshot["research_v4"] = study
    for key, data in (("candidates", flatten_candidates(study)), ("curves", study["curves"]), ("failures", study["failures"])):
        snapshot["queries"]["research_v4_" + key] = {"rows": data, "source": source,
            "methods": [{"language": "python", "code": "python research_v4.py --workers 6; then independent verify_v4.py; frozen protocol and selection files retained"}]}


def write_report(out, study):
    counts = Counter(c["readiness"] for c in study["candidates"])
    selected = next((c for c in study["candidates"] if c["experiment_id"] == study["selection"]["selected_id"]), None)
    lines = ["# V4大规模因子探索：每组公开结果与稳健性门槛", "",
             f"生成时间：{study['generated_at']}（Asia/Shanghai）。只读原feathers.zip。",
             f"{len(study['candidates'])}组配置，{study['protocol']['factor_spec_count']}个参数规格、{study['protocol']['idea_count']}个参数族；不等于独立idea数量。",
             f"2024选股计划独特签名{study['selection']['training_unique_signal_signatures']}个；完整等价组见冻结选型JSON。",
             "", "每组固定2024基准、2025基准、2025双倍费用/滑点、2025容量0.1%四项。统一10亿元、前日信号/次日开盘尝试、提前20源交易日退出；失败继续并逐项保留。",
             "2025此前已经观察，本轮全部属于同源回顾性探索；退出规则继承此前后验修复。不存在未触碰的独立2025确认样本。",
             "", "## 逐项可用状态", "", *[f"- {k}：{v}组" for k,v in sorted(counts.items())],
             "", "数值验收、研究门槛和真实市场可执行性为三件不同的事。缺失不填零；亏损、残仓、缺价减记、压力失败均保留。研究筛选通过仍不等于可实盘。",
             "", "## 2024冻结选型", "",
             "2024门槛：收益>0、平均实际仓位≥25%、回撤不超过40%、零减记且模型通过交易退出；按夏普、收益、ID排序。无人满足时不指定冠军。"]
    if selected:
        lines += ["", f"选中：`{selected['experiment_id']}`。其2025结果不用于替换冠军。", "",
                  "|情景|净收益|最大回撤|平均仓位|残仓(元)|减记(元)|", "|---|---:|---:|---:|---:|---:|"]
        for key in ("selection_2024", "evaluation_2025", "stress_2025", "capacity_2025"):
            r=selected[key]
            if r["status"] == "success": lines.append(f"|{key}|{r['total_return']:.4%}|{r['max_drawdown']:.4%}|{r['mean_invested']:.2%}|{r['terminal_position_value']:,.2f}|{r['writeoff_loss']:,.2f}|")
    else: lines.append("没有候选通过预定2024初筛，未指定冠军；仍完整评价与公开所有候选。")
    passing = [c for c in study["candidates"] if c["readiness"] == "通过研究筛选（仍有数据限制）"]
    passing.sort(key=lambda c: (-c["selection_2024"].get("sharpe", -math.inf), c["experiment_id"]))
    lines += ["", "## 通过研究门槛的候选（按2024夏普列示，最多20项）", "",
              "门槛：四项完整且独立数值验收通过、均无减记并模型卖出退出、2025三情景正收益、2025两个半年均正、容量场景仓位≥25%、各情景回撤≤40%。这是同源描述性筛选，未控制搜索产生的选择偏差。", "",
              "|候选|2025基准|加倍费用|容量0.1%|最大回撤(基准)|", "|---|---:|---:|---:|---:|"]
    for c in passing[:20]:
        lines.append(f"|{c['experiment_id']}|{c['evaluation_2025']['total_return']:.4%}|{c['stress_2025']['total_return']:.4%}|{c['capacity_2025']['total_return']:.4%}|{c['evaluation_2025']['max_drawdown']:.4%}|")
    if not passing: lines.append("|尚无候选同时满足全部门槛|—|—|—|—|")
    lines += ["", "## 适用边界", "", *[f"- {x}" for x in study["limitations"]],
              "", "## 文件", "", "- candidates.csv：每组参数、四情景指标、完整性和每项检查。",
              "- experiments.csv / runs/*/*/*/daily.csv：所有实际情景、17位有效数字日账本；失败record.json保留异常。",
              "- factor_catalog.json：每项公式、方向、所需历史、字段及边界。",
              "- selection_frozen_before_2025.json：2025评价前冻结的2024选择及等价签名。",
              "- validation.json：所有成功日账本的独立指标重算；逐笔重建仅限声明的detailed抽样。",
              "- detailed/：预定审计项的逐笔记录与作业延伸；不能把抽样逐笔核验称为全部候选逐笔核验。"]
    (out/"report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")


def finalize(out, study, records):
    from verify_v4 import validate_study
    validation = validate_study(out, study, records)
    study["validation"] = validation
    valid = set(validation["passed_run_keys"])
    for c in study["candidates"]:
        keys = ["|".join((c["experiment_id"], r, s)) for r,s in (("selection_2024","base"), ("evaluation_2025","base"), ("evaluation_2025","stress"), ("evaluation_2025","capacity"))]
        c["checks"]["numeric_pass"] = all(k in valid for k in keys)
        checks=c["checks"]
        for field,label in (("stress_2025","stress_pass"),("capacity_2025","capacity_pass")):
            r=c[field]
            scenario="stress" if field=="stress_2025" else "capacity"
            checks[label]=bool("|".join((c["experiment_id"],"evaluation_2025",scenario)) in valid and
                               r.get("status")=="success" and r.get("total_return",-math.inf)>0 and
                               r.get("writeoff_loss",1)==0 and r.get("exit_by_trades",False) and
                               r.get("max_drawdown",-math.inf)>=-.40 and
                               (scenario!="capacity" or r.get("mean_invested",0)>=.25))
        required=("all_required_runs_complete","numeric_pass","no_writeoff_all_scenarios","all_scenarios_exit_by_trades",
                  "positive_base_return","positive_stress_return","positive_capacity_return","positive_halves",
                  "capacity_mean_invested_ge25pct","max_drawdown_within40pct")
        if not checks["all_required_runs_complete"]: label="运行缺失或失败"
        elif not checks["numeric_pass"]: label="数值验收未通过"
        elif all(checks[k] for k in required): label="通过研究筛选（仍有数据限制）"
        elif checks["has_writeoff"]: label="含非成交减记"
        elif not checks["all_scenarios_exit_by_trades"]: label="容量或退出受限"
        else: label="收益或跨期稳定性未达门槛"
        c["readiness"]=label
    study["status"]="complete" if validation["passed"] and not study["failures"] else "complete_with_issues"
    study["snapshot_ready"]=bool(validation["passed"])
    study["generated_at"]=now()
    study["snapshot_revision"]=hashlib.sha256(json.dumps(finite_json(study),ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
    save_json(out/"result.json",finite_json(study))
    export=[]
    for c in flatten_candidates(study):
        export.append({k: json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in c.items()})
    pd.DataFrame(export).to_csv(out/"candidates.csv",index=False,encoding="utf-8-sig",float_format="%.17g")
    pd.DataFrame([{**{k:r.get(k) for k in ("experiment_id","factor_id","period_role","scenario","status","folder","error")},**r.get("metrics",{})} for r in records]).to_csv(out/"experiments.csv",index=False,encoding="utf-8-sig",float_format="%.17g")
    pd.DataFrame(study["curves"]).to_csv(out/"curves.csv",index=False,encoding="utf-8-sig",float_format="%.17g")
    write_report(out,study)
    if study["snapshot_ready"] and not study["protocol"]["pilot"]: save_json(LATEST,{"study_id":out.name,"snapshot_revision":study["snapshot_revision"]})
    return study


def run_study(workers=6, resume=None, pilot=False, retry_failed=False, progress=print):
    if type(workers) is not int or not 1<=workers<=6: raise ValueError("workers须为1至6整数")
    OUTPUT.mkdir(exist_ok=True)
    if LOCK_PATH.exists():
        lock=json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        try:
            import psutil
            active=psutil.pid_exists(int(lock["pid"])) and abs(psutil.Process(int(lock["pid"])).create_time()-lock["process_created"])<1
        except Exception: active=True
        if active: raise RuntimeError("已有V4进程持锁："+str(lock))
        LOCK_PATH.unlink()
    import psutil
    lock_handle=os.open(str(LOCK_PATH),os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    os.write(lock_handle,json.dumps({"pid":os.getpid(),"process_created":psutil.Process().create_time(),"started_at":now()}).encode())
    os.close(lock_handle)
    started=time.monotonic()
    out=None
    try:
        resource=reserve_performance_cores()
        if resume:
            out=Path(resume).resolve(); out.relative_to(OUTPUT.resolve())
            protocol=json.loads((out/"protocol.json").read_text(encoding="utf-8"))
            if protocol["code_sha256"]!=code_hashes(): raise RuntimeError("计算代码与冻结协议不同，拒绝混合恢复")
            if protocol["source_identity"]!=source_identity(): raise RuntimeError("原始ZIP身份发生变化，拒绝混合恢复")
            candidates=protocol["grid"]
        else:
            out=OUTPUT/datetime.now(ZoneInfo("Asia/Shanghai")).strftime("v4_%Y%m%d_%H%M%S")
            out.mkdir(exist_ok=False)
            candidates=grid()
            if pilot:
                ids={"reversal_w005","lowrange_w020","return_amount_corr_w020","average_trade_value_w020"}
                candidates=[c for c in candidates if c["factor_id"] in ids and c["universe"]=="liquid1000" and c["top_n"]==50 and c["rebalance"]=="monthly"]
            protocol={"frozen_at":now(),"study_id":out.name,"candidate_count":len(candidates),"factor_spec_count":len(FACTOR_SPECS),
                      "idea_count":len({r["idea_id"] for r in catalog()}),"planned_primary_runs":len(candidates)*4,
                      "input":"D:/quant/feathers.zip only; original unchanged", "source_identity":source_identity(),
                      "periods":PERIODS,"universes":UNIVERSES,"grid":candidates,
                      "base_config":asdict(base_config(wind_down_days=20)),"code_sha256":code_hashes(),"workers":workers,
                      "resource_policy":resource,"pilot":pilot,"selection_rule":"2024: positive return, invested>=25%, MDD>=-40%, no writeoff and trade-based exit; max Sharpe, then return, then ID; no fallback",
                      "all_candidates_scenarios":["2024/base","2025/base","2025/stress=2x fees/slippage","2025/capacity=0.1%"],
                      "st_truth_available":False,"opening_auction_verified":False,"maximum_numeric_money_tolerance":MONEY_TOL,
                      "failure_policy":"persist failure and continue remaining queue; resume skips existing successful records; explicit retry_failed retries failures",
                      "retrospective":True,"wind_down_days_frozen_before_this_run":20}
            save_json(out/"protocol.json",protocol)
            save_json(out/"factor_catalog.json",catalog())
        save_json(ACTIVE,{"study_id":out.name})
        state={"job_id":out.name,"job_kind":"research_v4","status":"running","phase":"selection_2024", "completed":0,
               "total":len(candidates)*4,"failures":0,"started_at":now(),"updated_at":now(),"elapsed_seconds":0.,"estimated_remaining_seconds":None}
        def update(phase,records,extra_total=0):
            elapsed=time.monotonic()-started
            state.update(phase=phase,completed=len(records),failures=sum(r["status"]!="success" for r in records),
                         total=len(candidates)*4+extra_total,elapsed_seconds=elapsed,updated_at=now(),
                         estimated_remaining_seconds=elapsed/max(1,len(records))*max(0,len(candidates)*4+extra_total-len(records)))
            save_json(out/"progress.json",finite_json(state))
            progress(json.dumps({k:state[k] for k in ("job_id","phase","completed","total","failures","elapsed_seconds","estimated_remaining_seconds")},ensure_ascii=False))
        records=[]; details=[]
        update("loading",records)
        groups={fid:[c for c in candidates if c["factor_id"]==fid] for fid in sorted({c["factor_id"] for c in candidates})}
        with ProcessPoolExecutor(max_workers=workers,initializer=_worker_init,initargs=(str(out),protocol)) as pool:
            for phase in ("selection_2024","evaluation_2025"):
                futures={pool.submit(_factor_group,(fid,phase,cs,retry_failed)):(fid,cs) for fid,cs in groups.items()}
                for future in as_completed(futures):
                    fid,cs=futures[future]
                    try: records.extend(future.result())
                    except Exception as exc:
                        # An individual group error never discards remaining groups.
                        for c in cs:
                            for scenario in (("base",) if phase=="selection_2024" else SCENARIOS):
                                folder=_record_path(out,c,phase,scenario);folder.mkdir(parents=True,exist_ok=True)
                                p=folder/"record.json"
                                if p.exists():
                                    try:
                                        records.append(json.loads(p.read_text(encoding="utf-8")));continue
                                    except (ValueError,OSError):
                                        p.replace(p.with_name("record.corrupt_"+str(time.time_ns())+".json"))
                                failure={"experiment_id":c["experiment_id"],"factor_id":fid,"period_role":phase,"scenario":scenario,"status":"failed","folder":folder.relative_to(out).as_posix(),"error":str(exc),"traceback":traceback.format_exc()}
                                save_json(p,failure);records.append(failure)
                    update(phase,records)
                if phase=="selection_2024":
                    selection=choose_training(candidates,records)
                    frozen=out/"selection_frozen_before_2025.json"
                    if frozen.exists():
                        prior=json.loads(frozen.read_text(encoding="utf-8"))
                        if any(prior[k]!=selection[k] for k in ("selected_id","shortlist","audit_ids")): raise RuntimeError("恢复后的2024选型与冻结内容不同")
                        selection=prior
                    else: save_json(frozen,selection)
            audits=[c for c in candidates if c["experiment_id"] in selection["audit_ids"]]
            extra_total=len(audits)*3+int(selection["selected_id"] in selection["audit_ids"])
            futures=[pool.submit(_detail_group,(c,c["experiment_id"]==selection["selected_id"],retry_failed)) for c in audits]
            for future in as_completed(futures):
                try: details.extend(future.result())
                except Exception as exc: details.append({"status":"failed","error":str(exc),"scenario":"detailed_group"})
                update("detailed_audits",records+details,extra_total)
        save_json(out/"run_records.json",records)
        study=assemble(out,protocol,candidates,selection,records,details)
        save_json(out/"result_pending_validation.json",finite_json(study))
        update("independent_validation",records+details,len(details))
        study=finalize(out,study,records)
        state.update(status=study["status"],phase="complete",updated_at=now(),completed=len(records)+len(details),
                     total=len(records)+len(details),estimated_remaining_seconds=0.,elapsed_seconds=time.monotonic()-started,
                     validation_passed=study["validation"]["passed"],snapshot_ready=study["snapshot_ready"])
        save_json(out/"progress.json",finite_json(state))
        progress("FINISHED "+str(out)+" "+study["status"])
        return study
    except Exception as exc:
        if out and out.exists():
            failure={"status":"interrupted","error":str(exc),"traceback":traceback.format_exc(),"at":now(),"resume":"python research_v4.py --resume "+str(out)}
            save_json(out/"interruption.json",failure)
            saved_state=locals().get("state",{"job_id":out.name,"job_kind":"research_v4"})
            saved_state.update(status="interrupted",error=str(exc),updated_at=now())
            save_json(out/"progress.json",saved_state)
        raise
    finally:
        if LOCK_PATH.exists():
            lock=json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            if lock.get("pid")==os.getpid(): LOCK_PATH.unlink()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers",type=int,default=6)
    parser.add_argument("--resume",type=Path)
    parser.add_argument("--pilot",action="store_true")
    parser.add_argument("--retry-failed",action="store_true")
    args=parser.parse_args()
    run_study(args.workers,args.resume,args.pilot,args.retry_failed)
