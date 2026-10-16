"""Post-hoc parameter-neighbor descriptions; never runs or changes a strategy.

CLI: python v4_diagnostics.py <study_dir>
Reads only that explicit directory's result.json and writes neighbor_diagnostics/.
The pure diagnose(study) function performs no filesystem IO.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from numbers import Real
from pathlib import Path
import statistics

AXES = ("window", "universe", "top_n", "rebalance")
SCENARIOS = {
    "training": "selection_2024",
    "evaluation": "evaluation_2025",
    "stress": "stress_2025",
    "capacity": "capacity_2025",
}
FAILED = {"failed", "error", "interrupted"}
PASS_LABEL = "通过研究筛选（仍有数据限制）"


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _axis(name, value):
    if name in ("window", "top_n"):
        return value if _finite(value) and value > 0 else None
    return value if isinstance(value, str) and value.strip() else None


def _value(candidate, field, metric):
    """The category partition always sums to expected; missing is never zero."""
    row = candidate.get(field)
    if not isinstance(row, dict):
        return "unknown", None
    if row.get("status") in FAILED:
        return "failed", None
    value = row.get(metric)
    if row.get("status") != "success" or not _finite(value):
        return "unknown", None
    return "observed", float(value)


def _stats(candidates, field, metric="total_return"):
    statuses = Counter()
    values = []
    for candidate in candidates:
        status, value = _value(candidate, field, metric)
        statuses[status] += 1
        if status == "observed":
            values.append(value)
    return {"expected": len(candidates), "observed": statuses["observed"],
            "failed": statuses["failed"], "unknown": statuses["unknown"],
            "positive_fraction": sum(value > 0 for value in values) / len(values) if values else None,
            "median": statistics.median(values) if values else None,
            "worst": min(values) if values else None, "best": max(values) if values else None}


def _flatten(prefix, stats):
    return {prefix + "_" + key: value for key, value in stats.items()}


def _scenario(stats):
    return {key: stats[key] for key in ("expected", "observed", "failed", "unknown", "positive_fraction")} | {
        "return_median": stats["median"], "return_worst": stats["worst"], "return_best": stats["best"]}


def _isolation(candidate, neighbors, stats):
    own_status, own = _value(candidate, "evaluation_2025", "total_return")
    observed = stats["observed"]
    complete = stats["expected"] > 0 and observed == stats["expected"]
    result = {"own_evaluation_status": own_status, "own_evaluation_return": own,
              "complete_neighbor_coverage": complete, "observed_positive_isolation": None,
              "isolated_positive_peak": None, "own_minus_neighbor_median": None,
              "fraction_observed_neighbors_not_higher": None}
    if own_status != "observed":
        status, text = "own_result_unavailable", "本项2025基准失败、缺失或非有限，不能判断参数孤立性。"
    elif stats["expected"] == 0:
        status, text = "no_single_axis_neighbors", "没有同参数族且恰改一个轴的条目，不能据此认定稳健或孤立。"
    elif not observed:
        status, text = "neighbors_unavailable", "符合定义的邻居均失败或未知，缺乏可比较的2025收益，不能判断孤立性。"
    else:
        values = [v for state, v in (_value(n, "evaluation_2025", "total_return") for n in neighbors) if state == "observed"]
        isolated = own > 0 and not any(v > 0 for v in values)
        delta = own - stats["median"]
        result.update(observed_positive_isolation=isolated, isolated_positive_peak=isolated if complete else None,
                      own_minus_neighbor_median=delta if math.isfinite(delta) else None,
                      fraction_observed_neighbors_not_higher=sum(v <= own for v in values) / len(values))
        if isolated:
            status = "isolated_positive_peak" if complete else "observed_isolation_incomplete_coverage"
            text = f"本项2025基准为正；{observed}个已观测邻居均非正，呈正收益孤立特征。"
        elif own > stats["best"]:
            status, text = "higher_than_observed_neighbors", "本项收益高于所有已观测邻居；邻居仍可能为正，不能仅凭相对高点认定孤峰。"
        elif own > 0:
            status, text = "positive_with_positive_neighbor", "本项和至少一个已观测邻居2025收益为正；这只是局部描述，不是稳健性确认。"
        else:
            status, text = "nonpositive_own_return", "本项2025收益非正；局部比较不构成盈利或稳健性证明。"
        if not complete:
            text += f" 邻域还含{stats['failed']}项失败和{stats['unknown']}项未知，不对完整邻域作断言。"
    result.update(isolation_status=status, isolation_description=text)
    return result


def diagnose(study, out=None):
    """Return descriptive tables, optionally exporting a new diagnostics folder.

    Neighbor = same idea_id and exactly one differing complete axis among
    window/universe/top_n/rebalance. All alternative levels on that axis count;
    there is no numerical nearest-neighbor or evaluation-return-based selection.
    Input selections, returns, readiness, and candidate dictionaries are never
    mutated. Failed/unknown neighbors remain in expected counts, not ratios.
    """
    if not isinstance(study, dict) or not isinstance(study.get("candidates"), list):
        raise ValueError("study.candidates must be an array")
    candidates = study["candidates"]
    ids = [candidate.get("experiment_id") if isinstance(candidate, dict) else None for candidate in candidates]
    if any(not isinstance(identity, str) or not identity.strip() for identity in ids) or len(set(ids)) != len(ids):
        raise ValueError("Candidate experiment_id values must be unique nonempty strings")
    families = defaultdict(list)
    coordinates, family_key, warnings = {}, {}, []
    for candidate in candidates:
        identity = candidate["experiment_id"]
        idea = candidate.get("idea_id")
        family_key[identity] = idea if isinstance(idea, str) and idea.strip() else "__missing_idea__:" + identity
        families[family_key[identity]].append(candidate)
        values = {name: _axis(name, candidate.get(name)) for name in AXES}
        coordinates[identity] = values
        if any(value is None for value in values.values()):
            warnings.append({"experiment_id": identity, "warning": "Incomplete axes: single-axis neighborhood cannot be fully established"})
        if not isinstance(idea, str) or not idea.strip():
            warnings.append({"experiment_id": identity, "warning": "Missing idea_id: never grouped with other unknown families"})
    records = []
    for candidate in candidates:
        identity = candidate["experiment_id"]
        values = coordinates[identity]
        neighbors = []
        changes = {}
        if all(value is not None for value in values.values()):
            for other in families[family_key[identity]]:
                other_id = other["experiment_id"]
                axes = coordinates[other_id]
                if other_id == identity or any(value is None for value in axes.values()):
                    continue
                differing = [axis for axis in AXES if values[axis] != axes[axis]]
                if len(differing) == 1:
                    neighbors.append(other)
                    changes[other_id] = differing[0]
        neighbors.sort(key=lambda c: c["experiment_id"])
        row = {"experiment_id": identity, "idea_id": candidate.get("idea_id"), **values,
               "neighbor_count": len(neighbors), "neighbor_ids": [n["experiment_id"] for n in neighbors],
               "neighbor_changed_axes": changes, "neighborhood_definition_complete": all(v is not None for v in values.values()),
               "posthoc_descriptive_only": True, "variants_are_not_independent_evidence": True}
        for prefix, field in SCENARIOS.items():
            stats = _stats(neighbors, field)
            row.update(_flatten(prefix, stats))
            row[prefix] = _scenario(stats)
        sharpe = _stats(neighbors, "selection_2024", "sharpe")
        row.update(_flatten("training_sharpe", sharpe))
        row["training"].update(sharpe_median=sharpe["median"], **{"sharpe_"+name: sharpe[name] for name in ("expected", "observed", "failed", "unknown")})
        isolation = _isolation(candidate, neighbors, _stats(neighbors, "evaluation_2025"))
        row.update(isolation)
        row["isolation"] = {"status": isolation["isolation_status"], "description": isolation["isolation_description"],
                            **{name: isolation[name] for name in ("own_evaluation_status", "own_evaluation_return", "complete_neighbor_coverage", "observed_positive_isolation", "isolated_positive_peak", "own_minus_neighbor_median", "fraction_observed_neighbors_not_higher")}}
        records.append(row)
    summaries = []
    for family, members in sorted(families.items()):
        complete = sum(all(isinstance(c.get(field), dict) and c[field].get("status") == "success" for field in SCENARIOS.values()) for c in members)
        windows = {coordinates[c["experiment_id"]]["window"] for c in members if coordinates[c["experiment_id"]]["window"] is not None}
        factors = {c.get("factor_id", c.get("factor")) for c in members if c.get("factor_id", c.get("factor")) is not None}
        statuses = Counter(c.get("readiness", "unknown") for c in members)
        row = {"idea_id": members[0].get("idea_id"), "family_key": family, "window_spec_count": len(windows),
               "factor_spec_count": len(factors), "configuration_count": len(members), "all_four_status_success_count": complete,
               "existing_research_pass_count": statuses[PASS_LABEL], "existing_readiness_counts": dict(statuses),
               "variants_are_not_independent_evidence": True, "posthoc_descriptive_only": True}
        for prefix, field in SCENARIOS.items():
            stats = _stats(members, field)
            row.update(_flatten(prefix, stats))
            row[prefix] = _scenario(stats)
        summaries.append(row)
    report = {"format": "daily-factor-neighbor-diagnostics", "version": 1,
              "study_id": study.get("study_id"), "source_snapshot_revision": study.get("snapshot_revision"),
              "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "posthoc": True, "changes_selection_or_readiness": False, "additional_simulations": 0,
              "neighbor_definition": "Same idea_id; exactly one of window/universe/top_n/rebalance differs, other three equal; all alternative axis levels counted.",
              "denominator_policy": "expected=observed+failed+unknown; positive fractions and numerical summaries use only successful finite metric values; absent denominators produce null.",
              "summary": {"candidate_count": len(candidates), "family_count": len(families),
                          "directed_neighbor_pairs": sum(r["neighbor_count"] for r in records),
                          "complete_positive_isolation_count": sum(r["isolated_positive_peak"] is True for r in records),
                          "incomplete_observed_isolation_count": sum(r["isolation_status"] == "observed_isolation_incomplete_coverage" for r in records),
                          "no_neighbor_count": sum(r["neighbor_count"] == 0 for r in records)},
              "neighbors": records, "family_summary": summaries, "families": summaries, "warnings": warnings,
              "limitations": ["2025 was already observed; this is post-hoc description, not independent confirmation or a new selection criterion.",
                              "Same-family parameter variants are correlated; configuration and positive-count totals are not independent pieces of evidence.",
                              "An equal-weight planned-basket signature is not proof of identical fills or a correlation estimate.",
                              "No factor score, market return, candidate selection, readiness, or existing result file is changed.",
                              "Historical ST truth, opening auction execution, physical settlement, and future profitability remain unverified."]}
    if out is not None:
        export(report, Path(out))
    return report


def export(report, out):
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("neighbor_diagnostics.csv", report["neighbors"]), ("family_summary.csv", report["families"])):
        fields = list(dict.fromkeys(key for row in rows for key in row))
        if not fields:
            fields = ["experiment_id" if name.startswith("neighbor") else "idea_id"]
        with (out / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    (out / "diagnostics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# V4参数邻域后验诊断", "", "本处理只描述已有结果，不新增模拟，不改变选型、收益或既定readiness。2025已经观察，不能把邻域表现包装为独立确认。", "",
             "邻居定义：同idea_id，window、universe、top_n、rebalance四轴恰有一轴不同，其余三轴相同；该轴所有其他档位均计入。", "",
             "分母：expected=observed+failed+unknown。比例只以成功且数值有限的observed为分母；无观测时留空/JSON null，绝不填0。2024夏普和收益的可用数量分开列示。", "",
             f"配置{report['summary']['candidate_count']}项，参数族{report['summary']['family_count']}个；有完整邻域观测的正收益孤立项{report['summary']['complete_positive_isolation_count']}项，覆盖不完整但已观测邻居呈孤立特征{report['summary']['incomplete_observed_isolation_count']}项。", "",
             "孤立标记只表示本项2025为正而已观测单轴邻居均非正，不是显著性检验。高于邻居不自动称孤峰，同族相邻变体数量不能当作独立证据。", "",
             "family_summary里的四项成功仅指既有status完整；研究pass计数照录既定readiness，不重新授予研究通过或真实市场可用标签。", "",
             "文件：neighbor_diagnostics.csv逐项邻域；family_summary.csv按idea_id聚合；diagnostics.json完整规则、分母和限制。", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    directory = args.study_dir.resolve()
    study = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    output = args.output.resolve() if args.output else directory / "neighbor_diagnostics"
    result = diagnose(study, output)
    print(json.dumps({"output": str(output), **result["summary"]}, ensure_ascii=False))
