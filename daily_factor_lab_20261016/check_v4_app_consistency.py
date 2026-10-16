"""Prepare a normal-app replay and compare existing artifacts; never simulate.

Standard library only. No Market, ZIP, latest pointer, progress, or active-job
reads. Invoke prepare only after the selected research records are final.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PERFORMANCE_FIELDS = {"total_return", "annual_return", "annual_vol", "sharpe", "max_drawdown", "days",
                      "final_nav", "costs", "annual_two_sided_turnover", "mean_invested", "trades",
                      "writeoff_loss", "max_stale_fraction"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def frozen_code(protocol):
    hashes = protocol["code_sha256"]
    require(bool(hashes), "Missing frozen compute hashes")
    for name, expected in hashes.items():
        path = (ROOT / name).resolve()
        require(path.parent == ROOT, "Unexpected compute filename")
        require(sha(path) == expected, "Frozen compute code differs: " + name)
    require(Path(protocol["source_identity"]["path"]).resolve() ==
            (ROOT.parent / "feathers.zip").resolve(), "Unexpected source path")


def record_check(record, experiment, role, scenario):
    require(record.get("status") == "success", "Research record is not successful")
    require((record.get("experiment_id"), record.get("period_role"), record.get("scenario"))
            == (experiment, role, scenario), "Research record identity differs")
    require(record.get("cost_multiplier") == (2 if scenario == "stress" else 1),
            "Research cost multiplier differs")


def prepare(protocol_path, base_path, output, detailed_path=None):
    protocol_path, base_path = Path(protocol_path).resolve(), Path(base_path).resolve()
    protocol, base = read_json(protocol_path), read_json(base_path)
    frozen_code(protocol)
    experiment, role = base["experiment_id"], base["period_role"]
    record_check(base, experiment, role, "base")
    require(role == "evaluation_2025", "This acceptance is for evaluation_2025")
    candidates = [c for c in protocol["grid"] if c["experiment_id"] == experiment]
    require(len(candidates) == 1, "Candidate absent or duplicated in frozen grid")
    candidate = candidates[0]
    require(base["factor_id"] == candidate["factor_id"], "Factor identity differs")
    stress_path = base_path.parent.parent / "stress" / "record.json"
    stress = read_json(stress_path)
    record_check(stress, experiment, role, "stress")
    require(base["config"] == stress["config"], "Base/stress configs differ")
    cfg = base["config"]
    pool = protocol["universes"][candidate["universe"]]
    start, end = protocol["periods"][role]
    expected = dict(protocol["base_config"])
    expected.update(start=start, end=end, top_n=candidate["top_n"], rebalance=candidate["rebalance"],
                    min_history=max(120, candidate["required_history"]), universe=pool["universe"],
                    liquidity_top_n=pool["liquidity_top_n"])
    require(cfg == expected, "Record config differs from frozen protocol/grid")
    require(cfg["execution_model"] == "daily_conservative", "Wrong execution model")
    require(cfg["top_n"] == candidate["top_n"] and cfg["rebalance"] == candidate["rebalance"],
            "Candidate selection differs from record config")
    strategy = {"format": "daily-factor-strategy", "version": 1,
                "name": candidate["name"] + " / " + candidate["universe_name"],
                "score": {"op": "v4_" + candidate["factor_id"], "args": [], "params": {}},
                "selection": {"top_n": cfg["top_n"], "rebalance": cfg["rebalance"], "direction": "desc"},
                "packs": []}
    references = {"protocol": str(protocol_path), "base": str(base_path), "stress": str(stress_path)}
    if detailed_path:
        detailed_path = Path(detailed_path).resolve()
        detail = read_json(detailed_path)
        record_check(detail, experiment, role, "base")
        require(detail.get("detailed") is True and detail["config"] == cfg,
                "Detailed reference config/flag differs")
        references["detailed_base"] = str(detailed_path)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", cfg)
    write_json(output / "strategy.json", strategy)
    manifest = {"format": "v4-normal-app-acceptance-plan", "version": 1,
                "experiment_id": experiment, "period_role": role, "factor_id": candidate["factor_id"],
                "references": references, "reference_sha256": {k: sha(v) for k, v in references.items()},
                "config": cfg, "strategy": strategy, "compute_sha256": protocol["code_sha256"],
                "source_identity_from_frozen_protocol": protocol["source_identity"],
                "source_zip_rehashed": False, "simulation_started": False}
    write_json(output / "manifest.json", manifest)
    # Explicit future launch only. Preserve two complete performance cores and
    # force Arrow to one thread after lab's import-time defaults.
    program = (
        "import os,sys,runpy; "
        "os.environ.update({k:'1' for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS')}); "
        f"os.environ['FACTOR_DATA_ZIP']={str(ROOT.parent / 'feathers.zip')!r}; "
        "from research import reserve_performance_cores; "
        "r=reserve_performance_cores(); print(r,flush=True); "
        "assert os.name!='nt' or len(r.get('reserved_logical_processors',[]))>=2, 'CPU reservation failed'; "
        "import pyarrow as pa; pa.set_cpu_count(1); pa.set_io_thread_count(1); "
        f"sys.argv=['run_experiments.py','--config',{str(output / 'config.json')!r},'--strategy',{str(output / 'strategy.json')!r},'--no-build']; "
        "runpy.run_path('run_experiments.py',run_name='__main__')"
    )
    quoted = "'" + program.replace("'", "''") + "'"
    launcher = ("# Run only after the research queue has completed.\n"
                "$ErrorActionPreference = 'Stop'\n"
                "Set-Location -LiteralPath '" + str(ROOT).replace("'", "''") + "'\n"
                "& python -c " + quoted + "\nexit $LASTEXITCODE\n")
    (output / "launch_after_completion.ps1").write_text(launcher, encoding="utf-8")
    return manifest


def csv_comparison(left, right):
    """Exact serialized cell comparison, preserving row order and all columns."""
    with Path(left).open(encoding="utf-8-sig", newline="") as a, Path(right).open(encoding="utf-8-sig", newline="") as b:
        first, second = list(csv.reader(a)), list(csv.reader(b))
    require(bool(first) and first[0] == second[0], "CSV headers differ: " + str(right))
    require(len(first) == len(second), "CSV row counts differ: " + str(right))
    for row, (x, y) in enumerate(zip(first[1:], second[1:]), 2):
        require(x == y, f"CSV cells differ at row {row}: {right}")
    return {"passed": True, "data_rows": len(first)-1, "columns": first[0], "comparison": "exact serialized cells in original order"}


def compare(manifest_path, app_run):
    manifest, app_run = read_json(manifest_path), Path(app_run).resolve()
    refs = manifest["references"]
    for key, path in refs.items():
        require(sha(path) == manifest["reference_sha256"][key], "Reference changed: " + key)
    frozen_code(read_json(refs["protocol"]))
    result = read_json(app_run / "result.json")
    require(result.get("status") == "complete" and not result.get("failures"), "App run incomplete or failed")
    require(result["config"] == manifest["config"] == read_json(app_run / "config.json"), "App config differs")
    require(result["custom_strategy"] == manifest["strategy"] == read_json(app_run / "strategy.json"), "App strategy differs")
    checks = {}
    for scenario in ("base", "stress"):
        record = read_json(refs[scenario])
        folder = Path(refs[scenario]).parent
        checks[scenario + "_daily"] = csv_comparison(folder / "daily.csv", app_run / scenario / "custom" / "daily.csv")
        rows = [r for r in result["summary"] if r["factor"] == "custom" and r["scenario"] == scenario]
        require(len(rows) == 1, "Missing/duplicate app metrics: " + scenario)
        names = set(rows[0]) - {"factor", "name", "scenario"}
        require(names == PERFORMANCE_FIELDS, "Missing/unexpected app metric fields")
        for name in names:
            require(rows[0][name] == record["metrics"][name], "Metric differs: " + scenario + "/" + name)
        checks[scenario + "_metrics"] = {"passed": True, "fields": sorted(names), "comparison": "exact JSON numeric values"}
    base = read_json(refs["base"])
    for app_name, research_name in (("blocked_attempts", "blocked"), ("terminal", "terminal"),
                                     ("execution_diagnostics", "execution_diagnostics")):
        require(read_json(app_run / "base" / "custom" / (app_name + ".json")) == base[research_name],
                "Base diagnostic differs: " + app_name)
        checks["base_" + app_name] = {"passed": True}
    if "detailed_base" in refs:
        for name in ("daily", "trades", "orders", "holdings"):
            checks["base_detailed_" + name] = csv_comparison(
                Path(refs["detailed_base"]).parent / (name + ".csv"), app_run / "base" / "custom" / (name + ".csv"))
    return {"format": "v4-normal-app-consistency", "passed": True, "experiment_id": manifest["experiment_id"],
            "app_run": str(app_run), "checks": checks, "simulation_started": False, "source_zip_read": False,
            "scope": "base/stress full daily rows and shared metrics; base diagnostics; detailed base ledgers only when supplied",
            "limitations": ["App does not save stress trade/order/holding ledgers; no stress ledger equality claim.",
                            "No source ZIP rehash or external market/fill verification.",
                            "Current compute hashes match the frozen protocol; app artifacts do not independently attest execution-time code hashes.",
                            "Equality proves replay consistency within the declared historical model, not independent profitability evidence."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    p = modes.add_parser("prepare")
    p.add_argument("--protocol", required=True)
    p.add_argument("--base-record", required=True)
    p.add_argument("--detailed-base-record")
    p.add_argument("--output", required=True)
    p = modes.add_parser("compare")
    p.add_argument("--manifest", required=True)
    p.add_argument("--app-run", required=True)
    p.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.protocol, args.base_record, args.output, args.detailed_base_record)
        print("Prepared only; no simulation launched: " + str(Path(args.output).resolve()))
        return 0
    try:
        report = compare(args.manifest, args.app_run)
    except (ValueError, OSError, KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        report = {"format": "v4-normal-app-consistency", "passed": False, "error": str(exc),
                  "simulation_started": False, "source_zip_read": False}
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
