"""Past-only cross-sectional information diagnostics for signed 5DR.

No I/O or calculation at import. Returns aggregate summaries; never exports panels.
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def hac_mean(values, lag=5):
    """Bartlett HAC of a mean on the original, uncompressed source-date grid."""
    x = np.asarray(values, dtype=float)
    ok = np.isfinite(x)
    n = int(ok.sum())
    out = {"n": n, "mean": float(x[ok].mean()) if n else None,
           "lag": int(lag), "se": None, "t": None, "p_raw": None,
           "ci95_low": None, "ci95_high": None}
    if n < 20:
        return out
    z = np.where(ok, x - out["mean"], 0.0)
    meat = float(z @ z)
    for k in range(1, min(int(lag), len(x) - 1) + 1):
        meat += 2 * (1 - k / (lag + 1)) * float(z[k:] @ z[:-k])
    # Finite-observation correction; gaps still occupy their source-date slots.
    variance = max(0.0, meat * n / (n - 1) / (n * n))
    se = math.sqrt(variance)
    out["se"] = se
    out["ci95_low"] = out["mean"] - 1.959963984540054 * se
    out["ci95_high"] = out["mean"] + 1.959963984540054 * se
    if se > 0:
        out["t"] = out["mean"] / se
        out["p_raw"] = math.erfc(abs(out["t"]) / math.sqrt(2))
    return out


def _rank(x):
    return pd.Series(x).rank(method="average").to_numpy(dtype=float)


def _corr(x, y):
    if len(x) < 30 or np.std(x) == 0 or np.std(y) == 0:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def _ols_beta(y, x):
    if len(y) < 30 or np.linalg.matrix_rank(x) < x.shape[1]:
        return np.nan
    return float(np.linalg.lstsq(x, y, rcond=None)[0][-1])


def _bounds(spec):
    if isinstance(spec, dict):
        return pd.Timestamp(spec["start"]), pd.Timestamp(spec["end"])
    return pd.Timestamp(spec[0]), pd.Timestamp(spec[1])


def _describe(values, horizon):
    a = np.asarray(values, dtype=float)
    b = a[np.isfinite(a)]
    out = hac_mean(a, max(5, int(horizon)))
    sd = float(np.std(b, ddof=1)) if len(b) > 1 else None
    out.update(std=sd, positive_fraction=float(np.mean(b > 0)) if len(b) else None,
               mean_over_std=out["mean"] / sd if sd and sd > 0 else None)
    if horizon == 5:
        out["hac_sensitivity"] = {str(k): hac_mean(a, k) for k in (10, 20)}
    return out


def evaluate_information(market, score, eligible_masks, controls, periods, output,
                         horizons=(1, 5, 10, 20), progress=None):
    """Write daily/decile/monthly aggregate CSVs and summary.json.

    Period ends are inclusive. Entry and exit must belong to the period; the
    signal may be the preceding source trading day. Source dates remain as NaN
    rows for inference. Controls use
    keys momentum20, lowvol20 and liquidity20. Output is a directory.
    """
    dates = pd.DatetimeIndex(market.dates)
    symbols = np.asarray(market.symbols, dtype=str)
    score = np.asarray(score, dtype=float)
    opens = np.asarray(market.open, dtype=float)
    raw_open = np.asarray(market.raw["open"], dtype=float)
    adj = np.asarray(market.raw["cumadj"], dtype=float)
    shape = (len(dates), len(symbols))
    arrays = [score, opens, raw_open, adj] + [np.asarray(v) for v in eligible_masks.values()]
    controls = {k: np.asarray(controls[k], dtype=float)
                for k in ("momentum20", "lowvol20", "liquidity20")}
    arrays += list(controls.values())
    if any(a.shape != shape for a in arrays):
        raise ValueError("All input matrices must match dates x symbols")
    if not dates.is_monotonic_increasing or dates.has_duplicates:
        raise ValueError("Source date grid must be unique and increasing")
    valid_open = np.isfinite(opens) & (opens > 0) & np.isfinite(raw_open) & (raw_open > 0) & np.isfinite(adj) & (adj > 0)
    bad_prefix = np.concatenate([np.zeros((1, shape[1]), dtype=np.int32),
                                 np.cumsum(~valid_open, axis=0, dtype=np.int32)], axis=0)
    rows, groups = [], []
    for period, spec in periods.items():
        start, end = _bounds(spec)
        # Include the preceding signal coordinate when its next source date is
        # the first in-period entry. Never compress the source calendar.
        entry_in_period = np.zeros(len(dates), dtype=bool)
        entry_in_period[:-1] = (dates[1:] >= start) & (dates[1:] <= end)
        indices = np.flatnonzero(entry_in_period)
        for universe, eligible in eligible_masks.items():
            eligible = np.asarray(eligible, dtype=bool)
            for h in horizons:
                h = int(h)
                if h < 1:
                    raise ValueError("Horizons must be positive integers")
                for t in indices:
                    entry, ex = t + 1, t + 1 + h
                    in_period = ex < len(dates) and dates[entry] >= start and dates[ex] <= end
                    sig = eligible[t] & np.isfinite(score[t])
                    ids = np.flatnonzero(sig)
                    n = len(ids)
                    unique_count = len(np.unique(score[t, ids]))
                    # Stable symbol tie-break is fixed before inspecting targets.
                    order = ids[np.lexsort((symbols[ids], score[t, ids]))]
                    decile = np.zeros(shape[1], dtype=np.int8)
                    percentile = np.full(shape[1], np.nan)
                    for g, chunk in enumerate(np.array_split(order, 10), 1):
                        decile[chunk] = g
                    if n:
                        percentile[ids] = (_rank(score[t, ids]) - .5) / n
                    y = np.full(shape[1], np.nan)
                    if in_period:
                        path = bad_prefix[ex + 1] == bad_prefix[entry]
                        with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                            returns = opens[ex] / opens[entry] - 1
                        good = path & np.isfinite(returns)
                        y[good] = returns[good]
                    paired = sig & np.isfinite(y)
                    count = int(paired.sum())
                    r = {"period": period, "universe": universe, "horizon": h,
                         "date": dates[t], "signal_date": dates[t],
                         "entry_date": dates[entry] if entry < len(dates) else pd.NaT,
                         "exit_date": dates[ex] if ex < len(dates) else pd.NaT,
                         "window_in_period": bool(in_period), "signal_count": n,
                         "unique_score_count": unique_count,
                         "unique_fraction": unique_count/n if n else np.nan,
                         "paired_count": count, "missing_count": n-count,
                         "coverage": count/n if n else np.nan,
                         "ic": np.nan, "rank_ic": np.nan, "raw_r5_ic": np.nan,
                         "raw_r5_rank_ic": np.nan, "beta": np.nan,
                         "p25_p75_difference": np.nan, "conditional_beta": np.nan,
                         "conditional_paired_count": 0, "spread_q10_q1": np.nan}
                    if count >= 30:
                        sx, sy = score[t, paired], y[paired]
                        r["ic"] = _corr(sx, sy)
                        r["rank_ic"] = _corr(_rank(sx), _rank(sy))
                        r["raw_r5_ic"], r["raw_r5_rank_ic"] = -r["ic"], -r["rank_ic"]
                        r["beta"] = _ols_beta(sy, np.column_stack([np.ones(count), percentile[paired]]))
                        r["p25_p75_difference"] = .5*r["beta"]
                    common = sig.copy()
                    for a in controls.values():
                        common &= np.isfinite(a[t])
                    common_ids = np.flatnonzero(common)
                    nc = len(common_ids)
                    if nc:
                        ranked = np.column_stack([(_rank(a[t, common_ids])-.5)/nc for a in controls.values()] + [(_rank(score[t, common_ids])-.5)/nc])
                        cp = np.isfinite(y[common_ids])
                        r["conditional_paired_count"] = int(cp.sum())
                        r["conditional_beta"] = _ols_beta(y[common_ids][cp], np.column_stack([np.ones(cp.sum()), ranked[cp]]))
                    means = {}
                    for g in range(1, 11):
                        assigned = decile == g
                        covered = assigned & np.isfinite(y)
                        ng, cg = int(assigned.sum()), int(covered.sum())
                        mean = float(y[covered].mean()) if cg else np.nan
                        means[g] = mean
                        groups.append({k:r[k] for k in ("period", "universe", "horizon", "date", "signal_date", "entry_date", "exit_date")} | {"decile":g, "assigned":ng, "covered":cg, "coverage":cg/ng if ng else np.nan, "mean_simple_return":mean})
                    if count >= 30 and unique_count > 1:
                        r["spread_q10_q1"] = means[10]-means[1]
                    rows.append(r)
                if progress:
                    progress(f"information {period} {universe} h={h} complete")
    daily = pd.DataFrame(rows)
    metrics = ("ic", "rank_ic", "spread_q10_q1", "beta", "p25_p75_difference", "conditional_beta")
    summary = {"method": {"signal":"signed 5DR=-R5; raw R5 ICs are algebraic sign reversals, not independent tests", "target":"O_adj[t+1+h]/O_adj[t+1]-1; full positive finite open/cumadj path", "groups":"past eligible finite signal pool; ascending score, symbol tie-break; array_split into 10 fixed groups", "percentile":"average rank minus 0.5 divided by past pool size", "inference":"source-grid Bartlett HAC, normal approximate 95% CI and raw two-sided P; min 20 finite days", "conditional":"association controlling only momentum20, lowvol20, liquidity20 ranks; no industry or size controls", "spread":"gross Q10-Q1 diagnostic, not shorting profits; no overlapping-return compounded NAV", "period_status":"all periods previously observed; no untouched out-of-sample claim", "min_pairs":30}, "periods":{}}
    monthly = []
    for (period, universe, h), frame in daily.groupby(["period", "universe", "horizon"], sort=False):
        result = {m:_describe(frame[m].to_numpy(), int(h)) for m in metrics}
        result["coverage"] = {"signal_total":int(frame.signal_count.sum()), "paired_total":int(frame.paired_count.sum()), "missing_total":int(frame.missing_count.sum())}
        inside = frame[frame.window_in_period]
        result["coverage"].update(
            in_period_signal_total=int(inside.signal_count.sum()),
            in_period_paired_total=int(inside.paired_count.sum()),
            in_period_missing_total=int(inside.missing_count.sum()),
            boundary_censored_signal_total=int(frame.loc[~frame.window_in_period, "signal_count"].sum()),
            all_total_definition="Legacy totals include boundary-censored signals; only in_period_missing_total represents missing within valid period windows")
        summary["periods"].setdefault(str(period), {}).setdefault(str(universe), {})[str(h)] = result
        if h == 5:
            for frequency, labels in (("month", frame.entry_date.dt.strftime("%Y-%m")), ("half_year", frame.entry_date.dt.year.astype(str)+"-H"+((frame.entry_date.dt.month-1)//6+1).astype(str))):
                for label, sub in frame.groupby(labels, sort=False):
                    for m in metrics:
                        monthly.append({"period":period,"universe":universe,"horizon":h,"frequency":frequency,"segment":label,"metric":m, **_describe(sub[m].to_numpy(), h)})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    daily.to_csv(output/"information_daily.csv", index=False, float_format="%.17g")
    pd.DataFrame(groups).to_csv(output/"information_deciles.csv", index=False, float_format="%.17g")
    # Nested sensitivity remains in JSON; tabular decomposition uses primary HAC.
    pd.DataFrame([{k:v for k,v in x.items() if k != "hac_sensitivity"} for x in monthly]).to_csv(output/"information_monthly.csv", index=False, float_format="%.17g")
    with (output/"summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return summary
