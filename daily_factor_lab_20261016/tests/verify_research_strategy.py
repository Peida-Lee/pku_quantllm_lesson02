"""Bounded real-source factor/AST/prefix checks. Never runs a backtest.

Run: python tests/verify_research_strategy.py [--output path]
The source ZIP is read only. Work is limited to a date/symbol submatrix and
single-thread numeric libraries. Local process affinity reserves two P cores.
"""
import os
for _key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_key] = "1"

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import research
from lab import Market, SOURCE, rebalance_days
from strategy_blocks import compile_strategy

pa.set_cpu_count(1)
pa.set_io_thread_count(1)
CUTOFF = pd.Timestamp("2025-06-30")
ATOL, RTOL = 1e-12, 1e-12


def load_submatrix():
    with zipfile.ZipFile(SOURCE) as archive:
        dates_table = feather.read_table(pa.BufferReader(archive.read("feathers/close.feather")))
        dates = pd.DatetimeIndex(dates_table["date"].to_numpy())
        symbols = dates_table.column_names[1:]
        chosen = sorted(set(np.linspace(0, len(symbols) - 1, 1600, dtype=int).tolist()
                            + [symbols.index(s) for s in ("000001.SZ", "600519.SH", "300750.SZ", "688981.SH") if s in symbols]))
        selected_symbols = [symbols[j] for j in chosen]
        date_mask = (dates >= "2024-07-01") & (dates <= "2025-12-31")
        raw = {}
        for field in ("open", "high", "low", "close", "amt", "cumadj", "num_trades"):
            if field == "close":
                table = dates_table
            else:
                table = feather.read_table(pa.BufferReader(archive.read("feathers/" + field + ".feather")), columns=["date"] + selected_symbols)
            field_dates = pd.DatetimeIndex(table["date"].to_numpy())
            if not field_dates.equals(dates):
                raise AssertionError("Source field dates differ")
            raw[field] = np.column_stack([table[s].to_numpy()[date_mask] for s in selected_symbols])
    market = Market.__new__(Market)
    market.source = str(SOURCE)
    market.dates = dates[date_mask]
    market.symbols = np.asarray(selected_symbols)
    market.raw = raw
    market.prepare()
    return market


def strategy_for(factor):
    label, _, expression = research.FACTOR_SPECS[factor]
    return {"format": "daily-factor-strategy", "version": 1, "name": label,
            "score": expression, "filter": research.node("is_finite", research.node("close")),
            "selection": {"top_n": 100, "rebalance": "weekly", "direction": "desc"}, "packs": []}


def same_numeric(a, b):
    a, b = np.asarray(a), np.asarray(b)
    finite_a, finite_b = np.isfinite(a), np.isfinite(b)
    same_finite = np.array_equal(finite_a, finite_b)
    joint = finite_a & finite_b
    error = float(np.max(np.abs(a[joint] - b[joint]), initial=0))
    return bool(same_finite and np.allclose(a, b, equal_nan=True, atol=ATOL, rtol=RTOL)), error


def target_signals(market, score, cfg, cutoff):
    """Compare frozen target construction without any execution/NAV simulation.

    Uses a fixed declared capital so this verifies score/rank/known-price target
    inputs, not realized holdings or portfolio NAV. Includes next-session
    targets whose previous-close signal date is at/before the cutoff.
    """
    indices = np.flatnonzero((market.dates >= cfg.start) & (market.dates <= cfg.end) & (np.arange(len(market.dates)) > 0))
    schedule = rebalance_days(market.dates, indices, cfg.rebalance)
    snapshots = []
    for i in indices:
        if i not in schedule or market.dates[i - 1] > cutoff:
            continue
        candidates = np.flatnonzero(np.isfinite(score[i-1]) & np.isfinite(market.close[i-1]) & (market.close[i-1] > 0))
        selected = candidates[np.lexsort((market.symbols[candidates], -score[i-1, candidates]))][:cfg.top_n]
        targets = cfg.capital * (1 - cfg.cash_buffer) / len(selected) / market.close[i-1, selected] if len(selected) else np.array([])
        snapshots.append((str(market.dates[i].date()), str(market.dates[i-1].date()), tuple(market.symbols[selected]), tuple(targets.tolist())))
    return snapshots


def run(output):
    affinity = research.reserve_performance_cores()
    market = load_submatrix()
    altered = Market.__new__(Market)
    altered.source, altered.dates, altered.symbols = market.source, market.dates.copy(), market.symbols.copy()
    altered.raw = {key: value.copy() for key, value in market.raw.items()}
    altered.prepare()
    configs = {name: research.base_config(universe=values["universe"], liquidity_top_n=values["liquidity_top_n"])
               for name, values in research.UNIVERSES.items()}
    # Prime caches before mutation, exercising prepare() cache invalidation too.
    for cfg in configs.values():
        altered.eligible(cfg)
    later = altered.dates > CUTOFF
    t = np.arange(int(later.sum()))[:, None]
    j = np.arange(len(altered.symbols))[None, :]
    future_price = 1.3 + .001 * t + .0001 * (j % 13)
    for field in ("open", "high", "low", "close"):
        altered.raw[field][later] *= future_price
    altered.raw["amt"][later] *= 2. + .01 * (j % 7)
    altered.raw["cumadj"][later] *= 1.1 + .001 * (j % 11)
    altered.prepare()
    cleared = not altered._eligible_cache and not altered._execution_metadata_cache
    prefix = market.dates <= CUTOFF
    matrices, future_matrices = research.factor_matrices(market), research.factor_matrices(altered)
    checks = []

    def check(name, passed, **evidence):
        checks.append({"name": name, "passed": bool(passed), **evidence})

    check("prepare_clears_preexisting_caches", cleared)
    for factor, values in matrices.items():
        passed, error = same_numeric(values[prefix], future_matrices[factor][prefix])
        check("raw_factor_prefix:" + factor, passed, max_absolute_error=error)
    coverage = {}
    signal_comparisons = 0
    for universe, cfg in configs.items():
        eligible, future_eligible = market.eligible(cfg), altered.eligible(cfg)
        check("eligible_prefix:" + universe, np.array_equal(eligible[prefix], future_eligible[prefix]))
        evaluation = (market.dates >= "2025-01-02") & (market.dates <= "2025-12-31")
        daily_count = eligible[evaluation].sum(axis=1)
        coverage[universe] = {"min": int(daily_count.min()), "median": float(np.median(daily_count)), "max": int(daily_count.max())}
        for factor in research.FACTOR_SPECS:
            candidate = {"factor": factor}
            score = research.score_for(candidate, eligible, matrices)
            future_score = research.score_for(candidate, future_eligible, future_matrices)
            compiled = compile_strategy(market, cfg, strategy_for(factor))
            compiled_future = compile_strategy(altered, cfg, strategy_for(factor))
            passed, error = same_numeric(score, compiled["score"])
            check("factor_AST_equivalence:" + universe + ":" + factor, passed, max_absolute_error=error)
            check("export_filter_eligible_equivalence:" + universe + ":" + factor, np.array_equal(eligible, compiled["eligible"]))
            passed, error = same_numeric(score[prefix], future_score[prefix])
            check("score_prefix:" + universe + ":" + factor, passed, max_absolute_error=error)
            passed, error = same_numeric(compiled["score"][prefix], compiled_future["score"][prefix])
            check("compiled_AST_prefix:" + universe + ":" + factor, passed, max_absolute_error=error)
            for count in (50, 100):
                for freq in ("weekly", "monthly"):
                    signal_cfg = research.base_config(universe=cfg.universe, liquidity_top_n=cfg.liquidity_top_n, top_n=count, rebalance=freq)
                    before = target_signals(market, score, signal_cfg, CUTOFF)
                    after = target_signals(altered, future_score, signal_cfg, CUTOFF)
                    check("frozen_target_prefix:" + universe + ":" + factor + ":n" + str(count) + ":" + freq,
                          before == after, rebalance_signals=len(before))
                    signal_comparisons += len(before)
    # One actual source stock, preferably a five-day window crossing a factor
    # change, independently calculated from the two raw closes and factors.
    col = int(np.flatnonzero(market.symbols == "000001.SZ")[0])
    candidates = np.flatnonzero((market.dates.year == 2025) & (np.arange(len(market.dates)) >= 5)
                               & np.isfinite(market.close[:, col]))
    candidates = [i for i in candidates if np.isfinite(market.close[i-5, col])]
    crossing = [i for i in candidates if market.raw["cumadj"][i, col] != market.raw["cumadj"][i-5, col]]
    index = (crossing or candidates)[0]
    close_t, factor_t = float(market.raw["close"][index, col]), float(market.raw["cumadj"][index, col])
    close_prev, factor_prev = float(market.raw["close"][index-5, col]), float(market.raw["cumadj"][index-5, col])
    manual = (close_t * factor_t) / (close_prev * factor_prev) - 1.
    observed = float(matrices["dr5_momentum"][index, col])
    reversed_observed = float(matrices["dr5_reversal"][index, col])
    check("real_stock_5DR_endpoint_hand_calculation", np.isclose(manual, observed, atol=ATOL, rtol=RTOL)
          and np.isclose(-manual, reversed_observed, atol=ATOL, rtol=RTOL), max_absolute_error=abs(manual - observed))
    manual_record = {"symbol": str(market.symbols[col]), "date_t": str(market.dates[index].date()), "date_t_minus_5_sessions": str(market.dates[index-5].date()),
                     "close_raw_t": close_t, "cumadj_t": factor_t, "close_raw_t_minus_5": close_prev, "cumadj_t_minus_5": factor_prev,
                     "formula": "(close_raw_t*cumadj_t)/(close_raw_t_minus_5*cumadj_t_minus_5)-1",
                     "manual_5DR": manual, "research_dr5_momentum": observed, "research_dr5_reversal": reversed_observed,
                     "crosses_factor_change": bool(crossing),
                     "direction": "momentum ranks larger signed 5DR first; reversal ranks negative 5DR first (larger prior losses preferred)",
                     "not_holding_period": "Five-session lookback; portfolio holding duration is determined separately by weekly/monthly rebalancing and fills."}
    result = {"passed": all(item["passed"] for item in checks), "checks": len(checks), "failed_checks": sum(not item["passed"] for item in checks),
              "validated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "source": str(SOURCE), "read_only_source": True,
              "subset": {"date_start": str(market.dates[0].date()), "date_end": str(market.dates[-1].date()), "dates": len(market.dates),
                         "symbols": len(market.symbols), "symbol_selection": "1600 deterministic evenly spaced source columns plus four named anchors",
                         "symbol_list_sha256": hashlib.sha256("\n".join(market.symbols.tolist()).encode()).hexdigest(),
                         "cutoff": str(CUTOFF.date()), "future_perturbed_fields": ["open", "high", "low", "close", "amt", "cumadj"]},
              "factor_count": len(research.FACTOR_SPECS), "universe_count": len(configs), "coverage": coverage,
              "target_signal_comparisons": signal_comparisons, "affinity": affinity, "numeric_threads": 1,
              "tolerance": {"absolute": ATOL, "relative": RTOL}, "manual_5DR": manual_record, "details": checks,
              "limits": ["No backtest or real model/provider call was run.", "This validates a deterministic real-data submatrix, not every stock on the full source axis.",
                         "Frozen target comparisons use fixed reference capital; they do not simulate realized portfolio NAV or executions.",
                         "No true historical ST labels are available; proxy accuracy is not established."]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "checks": result["checks"], "failed_checks": result["failed_checks"],
                      "dates": len(market.dates), "symbols": len(market.symbols), "report": str(output)}, ensure_ascii=False))
    for item in checks:
        if not item["passed"]:
            print(json.dumps(item, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "research_strategy_validation.json")
    args = parser.parse_args()
    raise SystemExit(run(args.output.resolve()))
