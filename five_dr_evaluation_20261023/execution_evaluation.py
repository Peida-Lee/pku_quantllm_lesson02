"""Fixed ZIP-only execution contrasts; does not select directions or parameters.

The caller supplies an already loaded old-engine Market. Detailed ledgers stay local.
"""
import os
for _key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_key] = "1"
import json
import sys
from dataclasses import asdict
from pathlib import Path
import numpy as np
import pandas as pd

OLD = Path(__file__).resolve().parent.parent / "daily_factor_lab_20261016"
if str(OLD) not in sys.path:
    sys.path.insert(0, str(OLD))
from lab import simulate, performance
from research import base_config
from v4_factors import FactorEngine
import pyarrow as pa
pa.set_cpu_count(1)
pa.set_io_thread_count(1)

PERIODS = {"evaluation_2025": {"start": "2025-01-02", "end": "2025-12-31", "role": "retrospective_evaluation"},
           "evaluation_2026sep": {"start": "2026-01-05", "end": "2026-09-30", "role": "retrospective_evaluation"}}
FACTORS = ("momentum_w020", "lowvol_w020", "liquidity_w020", "reversal_w005")
SCENARIOS = {"base": (1., .01), "double_cost": (2., .01), "capacity_0p1pct": (1., .001)}


def _clean(x):
    if isinstance(x, dict): return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (tuple, list)): return [_clean(v) for v in x]
    if isinstance(x, np.generic): return _clean(x.item())
    if isinstance(x, float) and not np.isfinite(x): return None
    return x


def _json(path, obj):
    Path(path).write_text(json.dumps(_clean(obj), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def cfgpool(market, cfg):
    """Legal current eligibility AND finite all four fixed components, using past only."""
    engine = FactorEngine(market, max_cache=0)
    pool = market.eligible(cfg).copy()
    for fid in FACTORS:
        pool &= np.isfinite(engine.compute(fid))
    engine.clear()
    return pool


def fixed_scores(market, cfg):
    """Rank each component within the identical current legal eligible universe.

    Complete component availability is required for composite scores; missing is
    never zero. Lag1 moves the entire raw 5DR score one source row backward in
    information time, then masks CURRENT eligibility: at execution i it uses
    reversal C[i-2]/C[i-7], versus C[i-1]/C[i-6] for ordinary 5DR.
    """
    eligible = market.eligible(cfg)
    common = cfgpool(market, cfg)
    engine = FactorEngine(market, max_cache=0)
    ranks = []
    reversal = None
    for fid in FACTORS:
        raw = engine.compute(fid)
        rank = pd.DataFrame(np.where(common, raw, np.nan)).rank(axis=1, pct=True, method="average").to_numpy()
        ranks.append(rank)
        if fid == "reversal_w005": reversal = raw.copy()
    base = (ranks[0] + ranks[1] + ranks[2]) / 3.
    added = (ranks[0] + ranks[1] + ranks[2] + ranks[3]) / 4.
    lag = np.full_like(reversal, np.nan)
    lag[1:] = reversal[:-1]
    scores = {"5dr": np.where(eligible, reversal, np.nan), "base_combo": base,
              "added_combo": added, "5dr_lag1": np.where(eligible, lag, np.nan)}
    engine.clear()
    return scores


def paired_increment(base, added, maxlags=5):
    """Added minus baseline daily net returns, same exact source calendar; HAC raw P."""
    import statsmodels.api as sm
    b = base.set_index("date")["return"]
    a = added.set_index("date")["return"]
    if not a.index.equals(b.index): raise ValueError("Paired strategy calendars differ")
    d = (a - b).to_numpy(dtype=float)
    if not np.all(np.isfinite(d)) or len(d) < 2: raise ValueError("Invalid paired returns")
    def sharpe(x):
        sd = np.std(x, ddof=1)
        return float(np.mean(x) / sd * np.sqrt(252)) if sd > 0 else None
    def mdd(x):
        nav = np.r_[1., np.cumprod(1 + np.asarray(x))]
        return float(np.min(nav / np.maximum.accumulate(nav) - 1))
    sd = np.std(d, ddof=1)
    hac_lag = min(maxlags, len(d)-1)
    hac_kwds = {"maxlags": hac_lag, "use_correction": False}
    fit = sm.OLS(d, np.ones((len(d), 1))).fit(cov_type="HAC", cov_kwds=hac_kwds, use_t=False)
    relative_fit = sm.OLS(a.to_numpy(), np.column_stack([np.ones(len(b)), b.to_numpy()])).fit(
        cov_type="HAC", cov_kwds=hac_kwds, use_t=False)
    alpha = float(relative_fit.params[0])
    bs, ads = sharpe(b), sharpe(a)
    bm, am = mdd(b), mdd(a)
    result = {"n_paired_days": len(d), "mean_daily_return_difference": float(d.mean()),
              "paired_active_return_annualized_IR_P4_minus_P3": float(d.mean()/sd*np.sqrt(252)) if sd > 0 else None,
              "base_sharpe": bs, "added_sharpe": ads,
              "sharpe_difference": ads-bs if ads is not None and bs is not None else None,
              "HAC_maxlags": hac_lag, "HAC_use_correction": False, "HAC_use_t": False,
              "HAC_mean_difference_CI95": fit.conf_int()[0].tolist(),
              "HAC_raw_P": float(fit.pvalues[0]), "base_max_drawdown": bm, "added_max_drawdown": am,
              "relative_three_factor_alpha": {
                  "model": "net_r_P4 = alpha + beta * net_r_P3; relative to fixed three-factor portfolio, not CAPM or official risk-model alpha",
                  "daily_alpha": alpha, "linear_annualized_alpha_252": alpha*252,
                  "beta": float(relative_fit.params[1]),
                  "HAC_daily_alpha_CI95": relative_fit.conf_int()[0].tolist(),
                  "HAC_linear_annualized_alpha_CI95": (relative_fit.conf_int()[0]*252).tolist(),
                  "HAC_alpha_raw_P": float(relative_fit.pvalues[0]),
                  "HAC_maxlags": hac_lag, "HAC_use_correction": False, "HAC_use_t": False},
              "paired_drawdown_change_added_minus_base": am-bm,
              "drawdown_change_interpretation": "positive means shallower added drawdown; descriptive paired paths, no risk-equivalence inference"}
    frame = pd.DataFrame({"date": b.index, "base_return": b.to_numpy(), "added_return": a.to_numpy(), "added_minus_base": d})
    return _clean(result), frame


def half_period_metrics(daily, capital):
    """Two consecutive halves of source-calendar days, each rebased at its own start."""
    out = {}
    cut = len(daily)//2
    for label, lo, hi in (("first_half", 0, cut), ("second_half", cut, len(daily))):
        sub = daily.iloc[lo:hi].copy()
        if sub.empty: continue
        initial = capital if lo == 0 else float(daily.nav.iloc[lo-1])
        out[label] = {"start": str(sub.date.iloc[0]), "end": str(sub.date.iloc[-1]), **performance(sub, initial)}
    return out


def calendar_half_metrics(daily, capital):
    """Calendar Jan-Jun / Jul-Dec, clipped to declared observed period.

    Rebase each segment at its own preceding NAV, retaining actual observed
    dates. Thus a 2026 September cutoff makes H2 July-September only.
    """
    dates = pd.to_datetime(daily["date"])
    labels = dates.dt.year.astype(str) + np.where(dates.dt.month <= 6, "_H1", "_H2")
    out = {}
    for label in labels.unique():
        positions = np.flatnonzero((labels == label).to_numpy())
        lo = int(positions[0])
        sub = daily.iloc[positions].copy()
        initial = capital if lo == 0 else float(daily.nav.iloc[lo-1])
        out[str(label)] = {"start": str(sub.date.iloc[0]), "end": str(sub.date.iloc[-1]),
                           "segmentation": "calendar Jan-Jun or Jul-Dec clipped to observed interval",
                           **performance(sub, initial)}
    return out


def run_execution(market, output, periods=None, progress=None):
    """Run 20 prespecified simulations (10 per period); exceptions skip and record.

    Fixed 1bn, top100, weekly long-only all_liquid / price_proxy, 20-day
    wind-down. No source loading, model search, parameter tuning or networking.
    """
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    periods = PERIODS if periods is None else periods
    summary = {"protocol": {"periods": periods, "factor_ids": FACTORS, "scenarios": SCENARIOS,
        "base_combo": "equal percentile ranks momentum20 (no skip), lowvol20, liquidity20",
        "added_combo": "four equal percentile ranks including -R5; common legal finite four-component pool; P3-to-P4 also changes original component weights, not a pure marginal addition", "expected_simulations": len(periods)*10,
        "daily_csv_float_format": "%.17g", "raw_P_only": True,
        "double_cost_semantics": "old simulator cost_multiplier=2 doubles commission/minimum, stamp, transfer and slippage; tick rounding may prevent exact realized doubling",
        "boundaries": ["Daily-bar model assumptions do not prove opening liquidity or auction fills.",
            "price_proxy is not verified historical ST exclusion.", "Writeoffs are losses, not trading exits.",
            "Synthetic total-return units are not physical settlement shares."]}, "runs": [], "paired": [], "failures": []}
    scores = fixed_scores(market, base_config(wind_down_days=20))
    for period, spec in periods.items():
        start, end = spec["start"], spec["end"]
        daily_cache = {}
        jobs = [(strategy, scenario) for scenario in SCENARIOS for strategy in ("5dr", "base_combo", "added_combo")]
        jobs.append(("5dr_lag1", "base"))
        for strategy, scenario in jobs:
            name = f"{period}__{strategy}__{scenario}"
            target = output / name
            target.mkdir(exist_ok=True)
            multiplier, capacity = SCENARIOS[scenario]
            cfg = base_config(start=start, end=end, wind_down_days=20,
                              max_participation=capacity, max_daily_participation=capacity)
            config = {**asdict(cfg), "cost_multiplier": multiplier, "keep_trades": scenario == "base",
                      "stamp_rate_sell": .0005, "transfer_rate_each_side": .00001, "strategy": strategy, "scenario": scenario}
            _json(target / "config.json", config)
            try:
                if progress: progress({"stage": "execution_start", "run": name})
                r = simulate(market, scores[strategy], cfg, cost_multiplier=multiplier, keep_trades=scenario == "base")
                for table in ("daily", "trades", "orders", "holdings"):
                    r[table].to_csv(target / f"{table}.csv", index=False, float_format="%.17g")
                _json(target / "terminal.json", r["terminal"])
                _json(target / "diagnostics.json", {k:r[k] for k in ("metrics", "blocked", "execution_diagnostics")})
                row = {"run": name, "period": period, "strategy": strategy, "scenario": scenario,
                       "config": config, "actual_start": str(r["daily"].date.iloc[0]), "actual_end": str(r["daily"].date.iloc[-1]),
                       "metrics": r["metrics"], "terminal": r["terminal"], "blocked": r["blocked"],
                       "blocked_attempts": sum(r["blocked"].values()),
                       "equal_trading_day_halves": half_period_metrics(r["daily"], cfg.capital),
                       "calendar_halves": calendar_half_metrics(r["daily"], cfg.capital)}
                summary["runs"].append(row)
                if strategy in ("base_combo", "added_combo"): daily_cache[(strategy, scenario)] = r["daily"]
                _json(target / "summary.json", row)
                if progress: progress({"stage": "execution_done", "run": name})
            except Exception as exc:
                failure = {"run": name, "error": repr(exc)}
                summary["failures"].append(failure)
                _json(target / "error.json", failure)
        for scenario in SCENARIOS:
            if all((s, scenario) in daily_cache for s in ("base_combo", "added_combo")):
                try:
                    paired, frame = paired_increment(daily_cache[("base_combo", scenario)], daily_cache[("added_combo", scenario)])
                    frame.to_csv(output / f"{period}__{scenario}__paired.csv", index=False, float_format="%.17g")
                    summary["paired"].append({"period": period, "scenario": scenario, **paired})
                except Exception as exc:
                    summary["failures"].append({"phase": "paired", "period": period,
                                                "scenario": scenario, "error": repr(exc)})
                    continue
        _json(output / "execution_summary.json", summary)
    summary["completed_simulations"] = len(summary["runs"])
    _json(output / "execution_summary.json", summary)
    return _clean(summary)
