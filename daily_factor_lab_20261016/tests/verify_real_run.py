"""Audit persisted base ledgers without rerunning a market simulation.

Usage: python tests/verify_real_run.py [outputs/run_id]
Writes real_run_validation.json beside the audited result.json.
"""
import json
import os
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather

ROOT = Path(__file__).resolve().parents[1]
FACTORS = ("momentum60", "reversal5", "lowvol20", "liquidity20", "combo")
CASH_TOL = 1e-5
FEE_TOL = 1e-7


def maxabs(values):
    return float(np.max(np.abs(np.asarray(values)), initial=0))


def audit(run_dir):
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(Path(os.environ.get("FACTOR_DATA_ZIP", str(ROOT.parent / "feathers.zip")))) as archive:
        source = feather.read_table(pa.BufferReader(archive.read("feathers/close.feather")), columns=["date"])
        source_dates = pd.DatetimeIndex(source["date"].to_numpy())
    expected_dates = source_dates[(source_dates >= cfg["start"]) & (source_dates <= cfg["end"]) & (np.arange(len(source_dates)) > 0)]
    report = {
        "run_id": run_dir.name,
        "validated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "scope": "Five persisted base scenarios; independent CSV accounting and source date coverage; no simulation rerun",
        "cash_tolerance_yuan": CASH_TOL, "fee_tolerance_yuan": FEE_TOL,
        "configured_start": cfg["start"], "configured_end": cfg["end"],
        "source_first_date": str(source_dates[0].date()), "source_last_date": str(source_dates[-1].date()),
        "expected_first_date": str(expected_dates[0].date()), "expected_last_date": str(expected_dates[-1].date()),
        "expected_trading_days": len(expected_dates), "strategies": {},
        "limitations": [
            "Does not prove opening auction liquidity or actual fills; daily bars cannot establish this.",
            "Adjusted units and raw share equivalents remain synthetic corporate-action accounting.",
            "WRITE_OFF is a configured missing-data valuation policy, not verified legal delisting status.",
            "No brokerage, external transfer, or real order is used."
        ],
    }
    all_pass = True
    for factor in FACTORS:
        directory = run_dir / "base" / factor
        daily = pd.read_csv(directory / "daily.csv")
        ledger = pd.read_csv(directory / "trades.csv")
        holdings = pd.read_csv(directory / "holdings.csv")
        checks = []

        def check(name, passed, **evidence):
            nonlocal all_pass
            passed = bool(passed)
            checks.append({"name": name, "passed": passed, **evidence})
            all_pass = all_pass and passed

        dates = pd.DatetimeIndex(pd.to_datetime(daily.date))
        check("source_date_coverage_exact", dates.equals(expected_dates), rows=len(daily),
              actual_first_date=str(dates[0].date()), actual_last_date=str(dates[-1].date()))
        check("daily_dates_sorted_unique", dates.is_monotonic_increasing and dates.is_unique)
        check("daily_cash_nonnegative", np.isfinite(daily.cash).all() and daily.cash.min() >= -CASH_TOL,
              min_cash=float(daily.cash.min()))
        check("daily_nav_finite_nonnegative", np.isfinite(daily.nav).all() and daily.nav.min() >= -CASH_TOL,
              min_nav=float(daily.nav.min()))
        check("daily_returns_finite", np.isfinite(daily["return"]).all())
        check("ledger_dates_within_daily_calendar", ledger.date.isin(daily.date).all())
        check("ledger_dates_nondecreasing", pd.to_datetime(ledger.date).is_monotonic_increasing)
        check("ledger_sides_known", ledger.side.isin(["BUY", "SELL", "WRITE_OFF"]).all())
        trade = ledger[ledger.side.isin(["BUY", "SELL"])].copy()
        bought = trade[trade.side == "BUY"]
        written = ledger[ledger.side == "WRITE_OFF"]
        check("signals_strictly_precede_execution", (pd.to_datetime(trade.signal_date) < pd.to_datetime(trade.date)).all())
        star = bought.symbol.str.startswith(("688", "689"))
        main = bought.loc[~star, "raw_share_equivalent"].to_numpy()
        star_qty = bought.loc[star, "raw_share_equivalent"].to_numpy()
        check("main_board_buy_hundred_share_lots", np.all(main >= 100 - 1e-7) and np.allclose(main / 100, np.round(main / 100), atol=1e-8, rtol=0),
              checked_buy_orders=int(len(main)))
        check("star_board_buy_at_least_200_integer_shares", np.all(star_qty >= 200 - 1e-7) and np.allclose(star_qty, np.round(star_qty), atol=1e-7, rtol=0),
              checked_buy_orders=int(len(star_qty)))
        check("positive_trade_notionals_and_quantities", (trade.notional > 0).all() and (trade.adjusted_units > 0).all() and (trade.raw_share_equivalent > 0).all())
        raw_notional_error = maxabs(trade.notional - trade.raw_share_equivalent * trade.open_raw)
        check("notional_equals_raw_open_times_share_equivalent", raw_notional_error <= CASH_TOL, max_absolute_error=raw_notional_error)

        fee_columns = ["commission", "slippage", "stamp", "transfer"]
        expected_total = trade[fee_columns].sum(axis=1)
        fee_parts_error = maxabs(trade.total - expected_total)
        check("fee_components_sum_to_total", fee_parts_error <= FEE_TOL, max_absolute_error=fee_parts_error)
        tdates = pd.to_datetime(trade.date)
        commission = np.maximum(float(cfg["min_commission"]), float(cfg["commission"]) * trade.notional)
        slippage = float(cfg["slippage"]) * trade.notional
        stamp = np.where(trade.side == "SELL", np.where(tdates < pd.Timestamp("2023-08-28"), .001, .0005), 0.) * trade.notional
        transfer = np.where(tdates < pd.Timestamp("2022-04-29"), .00002, .00001) * trade.notional
        expected_fees = np.column_stack([commission, slippage, stamp, transfer])
        independent_fee_error = float(np.max(np.abs(trade[fee_columns].to_numpy() - expected_fees), initial=0))
        check("independently_recomputed_date_specific_fees", independent_fee_error <= FEE_TOL, max_absolute_error=independent_fee_error)
        fill_expected = trade.open_raw * (1 + np.where(trade.side == "BUY", 1., -1.) * float(cfg["slippage"]))
        check("displayed_fill_price_matches_slippage", np.allclose(trade.fill_price_raw, fill_expected, atol=1e-8, rtol=0))
        costs_by_day = trade.groupby("date").total.sum().reindex(daily.date, fill_value=0).to_numpy()
        max_daily_cost_error = maxabs(costs_by_day - daily.cost)
        check("daily_costs_reconcile_to_trade_ledger", max_daily_cost_error <= CASH_TOL,
              max_absolute_error=max_daily_cost_error, total_ledger_fees=float(trade.total.sum()), total_daily_fees=float(daily.cost.sum()))
        counts = trade.groupby("date").size().reindex(daily.date, fill_value=0).to_numpy()
        check("daily_trade_counts_exclude_writeoffs", np.array_equal(counts, daily.trades.to_numpy()), trade_rows=int(len(trade)), writeoff_rows=int(len(written)))
        check("writeoffs_have_no_fee_or_signal", (written.total.fillna(0) == 0).all() and written.signal_date.fillna("").eq("").all())
        writeoffs_by_day = written.groupby("date").notional.sum().reindex(daily.date, fill_value=0).to_numpy()
        check("daily_writeoff_losses_reconcile", np.allclose(writeoffs_by_day, daily.writeoff_loss, atol=CASH_TOL, rtol=0), total_writeoffs=float(written.notional.sum()))

        cash = float(cfg["capital"])
        cash_after_errors = []
        buying_days = set()
        forbidden_same_day_sales = []
        balances = {}
        negative_positions = []
        for row in ledger.itertuples(index=False):
            key = (row.date, row.symbol)
            if row.side == "BUY":
                cash -= row.notional + row.total
                buying_days.add(key)
                balances[row.symbol] = balances.get(row.symbol, 0.) + row.adjusted_units
            elif row.side == "SELL":
                cash += row.notional - row.total
                if key in buying_days:
                    forbidden_same_day_sales.append(key)
                balances[row.symbol] = balances.get(row.symbol, 0.) - row.adjusted_units
                if balances[row.symbol] < -1e-6:
                    negative_positions.append((row.date, row.symbol, balances[row.symbol]))
            else:
                balances[row.symbol] = 0.
            cash_after_errors.append(abs(cash - row.cash_after))
        max_cash_after_error = max(cash_after_errors, default=0.)
        check("cash_after_each_ledger_row_recomputed", max_cash_after_error <= CASH_TOL, max_absolute_error=max_cash_after_error)
        flow = np.where(ledger.side == "BUY", -ledger.notional - ledger.total,
                        np.where(ledger.side == "SELL", ledger.notional - ledger.total, 0.))
        daily_flow = pd.Series(flow, index=ledger.date).groupby(level=0).sum().reindex(daily.date, fill_value=0).to_numpy()
        expected_cash = float(cfg["capital"]) + np.cumsum(daily_flow)
        max_cash_daily_error = maxabs(expected_cash - daily.cash)
        check("daily_cash_recomputed_from_starting_capital", max_cash_daily_error <= CASH_TOL, max_absolute_error=max_cash_daily_error)
        check("no_same_day_sale_after_buy", not forbidden_same_day_sales, violations=forbidden_same_day_sales[:5])
        check("no_negative_adjusted_position_in_ledger", not negative_positions, violations=negative_positions[:5])
        final_balances = {symbol: qty for symbol, qty in balances.items() if qty > 1e-8}
        final_holds = dict(zip(holdings.symbol, holdings.adjusted_units))
        position_error = max((abs(final_balances.get(k, 0.) - final_holds.get(k, 0.)) for k in set(final_balances) | set(final_holds)), default=0.)
        check("final_adjusted_positions_reconcile", position_error <= 1e-6, max_absolute_error=float(position_error))
        final_value_error = abs(float(holdings.value.sum()) + float(daily.cash.iloc[-1]) - float(daily.nav.iloc[-1]))
        check("final_cash_plus_holding_values_equals_nav", final_value_error <= CASH_TOL, absolute_error=final_value_error)
        summary = next(x for x in result["summary"] if x["factor"] == factor and x["scenario"] == "base")
        check("summary_matches_persisted_ledger_and_daily", abs(summary["costs"] - float(trade.total.sum())) <= CASH_TOL
              and abs(summary["final_nav"] - float(daily.nav.iloc[-1])) <= CASH_TOL
              and int(summary["trades"]) == len(trade) and int(summary["days"]) == len(daily))
        report["strategies"][factor] = {"passed": all(c["passed"] for c in checks), "checks": checks,
                                           "days": len(daily), "trades": len(trade), "writeoffs": len(written)}
    report["passed"] = all_pass
    report["check_count"] = sum(len(v["checks"]) for v in report["strategies"].values())
    report["failed_check_count"] = sum(not c["passed"] for v in report["strategies"].values() for c in v["checks"])
    target = run_dir / "real_run_validation.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"passed": all_pass, "checks": report["check_count"], "failed_checks": report["failed_check_count"], "report": str(target)}, ensure_ascii=False))
    return 0 if all_pass else 1


if __name__ == "__main__":
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "outputs" / json.loads((ROOT / "outputs" / "latest.json").read_text())["run_id"]
    raise SystemExit(audit(directory.resolve()))
