"""Audit a specified completed research study without importing its simulator.

Usage: python tests/verify_research.py research_outputs/study_ID
Reads the original ZIP for independent prices, amounts, dates, and factors.
Writes validation.json only; never changes a result, pointer, or research rule.
"""
import os
for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(key, "4")
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import traceback
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather

pa.set_cpu_count(4)
pa.set_io_thread_count(2)
PROJECT = Path(__file__).resolve().parents[1]
MONEY_TOL = .001  # one tenth of a cent; actual maxima are reported separately
FACTORS = {"dr5_momentum", "dr5_reversal", "reversal1", "momentum20", "momentum60", "lowvol20", "liquidity20", "small_amount20", "lowrange20", "reversal_lowvol"}
PERIODS = {"selection_2024": ("2024-01-02", "2024-12-31"), "evaluation_2025": ("2025-01-02", "2025-12-31"), "assignment_2025_2026sep": ("2025-01-02", "2026-09-30")}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def frame(path):
    try:
        return pd.read_csv(path, dtype={"symbol": str, "date": str, "signal_date": str}, float_precision="round_trip")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def truth(value):
    return value is True or str(value).lower() in {"true", "1"}


def safe_json(value):
    if isinstance(value, dict): return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [safe_json(v) for v in value]
    if isinstance(value, (float, np.floating)): return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer): return int(value)
    return value


def maxerr(left, right):
    a, b = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    return float(np.max(np.abs(a - b), initial=0)) if np.isfinite(a).all() and np.isfinite(b).all() else math.inf


def metric_values(daily, capital):
    nav = daily.nav.to_numpy(float)
    returns = nav / np.r_[capital, nav[:-1]] - 1
    std = returns.std(ddof=1) if len(returns) > 1 else 0
    peaks = np.maximum.accumulate(np.r_[capital, nav])[1:]
    return {"days": len(nav), "final_nav": nav[-1], "total_return": nav[-1] / capital - 1,
            "annual_return": (nav[-1] / capital) ** (252 / len(nav)) - 1 if nav[-1] > 0 else -1,
            "annual_vol": std * math.sqrt(252), "sharpe": returns.mean() / std * math.sqrt(252) if std else 0,
            "max_drawdown": np.min(nav / peaks - 1), "costs": math.fsum(daily.cost),
            "mean_invested": daily.invested_fraction.mean(), "trades": daily.trades.sum(),
            "writeoff_loss": math.fsum(daily.writeoff_loss), "max_stale_fraction": daily.stale_fraction.max(),
            "annual_two_sided_turnover": math.fsum(daily.turnover) * 252 / len(daily)}


class Source:
    def __init__(self, source, symbols):
        self.raw = {}
        self.symbols = sorted(symbols)
        self.symbol_index = {s: i for i, s in enumerate(self.symbols)}
        with zipfile.ZipFile(source) as z:
            for field in ("close", "open", "amt", "cumadj"):
                table = feather.read_table(pa.BufferReader(z.read(f"feathers/{field}.feather")), columns=["date", *self.symbols])
                observed = np.asarray(table["date"].to_numpy()).astype("datetime64[D]").astype(str)
                if not self.raw:
                    self.dates = observed
                    self.date_index = {d: i for i, d in enumerate(observed)}
                elif not np.array_equal(self.dates, observed):
                    raise ValueError("Original source matrix dates disagree")
                self.raw[field] = np.column_stack([table[s].to_numpy() for s in self.symbols]) if self.symbols else np.empty((len(observed), 0))
        factors = self.raw["cumadj"]
        valid = np.isfinite(factors) & (factors > 0)
        first = valid.argmax(axis=0)
        self.base = factors[first, np.arange(len(self.symbols))]

    def price(self, field, date, symbol):
        i, j = self.date_index[date], self.symbol_index[symbol]
        return float(self.raw[field][i, j])

    def scale(self, date, symbol):
        return self.price("cumadj", date, symbol) / self.base[self.symbol_index[symbol]]

    def capacities(self, date, symbol, cfg):
        i, j = self.date_index[date], self.symbol_index[symbol]
        window = self.raw["amt"][max(0, i - 20):i, j]
        prior = float(np.mean(window)) if len(window) == 20 and np.isfinite(window).all() else 0.
        current = self.raw["amt"][i, j]
        current = max(0., float(current)) if np.isfinite(current) else 0.
        return max(0., prior * cfg["max_participation"]), current * cfg["max_daily_participation"], current


class Audit:
    def __init__(self, directory, source, allow_quick):
        self.out, self.source, self.allow_quick = directory, source, allow_quick
        self.checks, self.ledger_reports = [], []
        self.metric_cache = {}

    def check(self, name, passed, **detail):
        self.checks.append({"check": name, "passed": bool(passed), **detail})
        print(("PASS " if passed else "FAIL ") + name, flush=True)

    def detail(self, row, source):
        cfg = row["config"]
        key = (row["experiment_id"], row["period_role"], row["scenario"])
        folder = self.out / row.get("folder", str(Path("runs") / key[0] / key[1] / key[2]))
        folder.resolve().relative_to(self.out.resolve())
        daily, trades, holdings, orders = [frame(folder / f"{name}.csv") for name in ("daily", "trades", "holdings", "orders")]
        diag = load(folder / "diagnostics.json")
        cash, last_nav = float(cfg["capital"]), float(cfg["capital"])
        units, marks, last_buy = defaultdict(float), defaultdict(float), {}
        errors = defaultdict(float)
        violations = Counter()
        multiplier = float(row.get("cost_multiplier", 0. if key[2] == "gross" else 2. if key[2] == "stress" else 1.))
        wind_days = int(cfg.get("wind_down_days", 0))
        wind_dates = set(daily.date.iloc[-min(wind_days, len(daily)):]) if wind_days else set()
        trade_days = {d: group.to_dict("records") for d, group in trades.groupby("date", sort=False)} if not trades.empty else {}
        order_days = {d: group.to_dict("records") for d, group in orders.groupby("date", sort=False)} if not orders.empty else {}
        for rec in daily.to_dict("records"):
            date = rec["date"]
            def mark(symbol, field):
                value = source.price(field, date, symbol) * source.scale(date, symbol)
                return value if math.isfinite(value) and value > 0 else None
            open_value = math.fsum(q * (mark(s, "open") or marks[s]) for s, q in units.items() if q > 1e-9)
            open_nav = cash + open_value
            used, day_fees, day_notional, execution_amounts, writeoffs = defaultdict(float), [], [], [], []
            fills, expired = [], []
            for trade in trade_days.get(date, []):
                if trade["side"] == "WRITE_OFF":
                    expired.append(trade)
                    errors["writeoff_cash_after"] = max(errors["writeoff_cash_after"], abs(cash - trade["cash_after"]))
                    continue
                fills.append(trade)
                symbol, side = trade["symbol"], trade["side"]
                q, raw_q, notional = float(trade["adjusted_units"]), float(trade["raw_share_equivalent"]), float(trade["notional"])
                raw_open, fill = source.price("open", date, symbol), float(trade["fill_price_raw"])
                execution_notional = raw_q * fill
                if not trade["signal_date"] < date: violations["nonlagged_trade_signal"] += 1
                if q <= 0 or raw_q <= 0 or side not in {"BUY", "SELL"}: violations["invalid_trade_quantity_or_side"] += 1
                if side == "SELL" and (last_buy.get(symbol) == date or q > units[symbol] + 1e-7): violations["T1_or_short_sale"] += 1
                if side == "BUY" and truth(trade.get("terminal", False)): violations["terminal_buy"] += 1
                if side == "BUY" and date in wind_dates: violations["buy_inside_preplanned_wind_down"] += 1
                if wind_days and truth(trade.get("wind_down", False)) != (date in wind_dates): violations["trade_wind_down_flag"] += 1
                errors["notional_from_source_open"] = max(errors["notional_from_source_open"], abs(raw_open * raw_q - notional))
                errors["raw_share_from_adjusted_units"] = max(errors["raw_share_from_adjusted_units"], abs(q * source.scale(date, symbol) - raw_q))
                errors["recorded_open_from_source"] = max(errors["recorded_open_from_source"], abs(raw_open - float(trade["open_raw"])))
                errors["execution_amount_from_tick_fill"] = max(errors["execution_amount_from_tick_fill"], abs(execution_notional - float(trade["execution_notional"])))
                expected = {"commission": max(cfg["min_commission"], execution_notional * cfg["commission"]) * multiplier,
                            "stamp": execution_notional * (.001 if date < "2023-08-28" else .0005) * multiplier if side == "SELL" else 0.,
                            "transfer": execution_notional * (.00002 if date < "2022-04-29" else .00001) * multiplier,
                            "slippage": abs(fill - raw_open) * raw_q}
                for fee, value in expected.items(): errors["fee_" + fee] = max(errors["fee_" + fee], abs(float(trade[fee]) - value))
                errors["fee_total"] = max(errors["fee_total"], abs(float(trade["total"]) - math.fsum(expected.values())))
                # Independent cash flow: actual fill proceeds/cost plus cash fees.
                # Do not subtract slippage again: it is already in the fill price.
                cash_fees = expected["commission"] + expected["stamp"] + expected["transfer"]
                cash += execution_notional - cash_fees if side == "SELL" else -execution_notional - cash_fees
                units[symbol] += -q if side == "SELL" else q
                if side == "BUY": last_buy[symbol] = date
                errors["trade_cash_after"] = max(errors["trade_cash_after"], abs(cash - trade["cash_after"]))
                used[symbol] += execution_notional
                day_fees.append(float(trade["total"]))
                day_notional.append(notional)
                execution_amounts.append(execution_notional)
            for symbol, quantity in units.items():
                if quantity > 1e-9:
                    marks[symbol] = mark(symbol, "close") or mark(symbol, "open") or marks[symbol]
            for trade in expired:
                symbol = trade["symbol"]
                loss = units[symbol] * marks[symbol]
                errors["writeoff_value"] = max(errors["writeoff_value"], abs(loss - float(trade["notional"])))
                writeoffs.append(loss)
                units[symbol] = 0.
            nav = cash + math.fsum(q * marks[s] for s, q in units.items() if q > 1e-9)
            for name, value in (("cash", cash), ("nav", nav), ("cost", math.fsum(day_fees)), ("writeoff_loss", math.fsum(writeoffs)), ("daily_capacity_used", math.fsum(execution_amounts))):
                if name in rec: errors["daily_" + name] = max(errors["daily_" + name], abs(float(rec[name]) - value))
            errors["daily_return"] = max(errors["daily_return"], abs(float(rec["return"]) - (nav / last_nav - 1)))
            errors["daily_turnover"] = max(errors["daily_turnover"], abs(float(rec["turnover"]) - (math.fsum(day_notional) / open_nav if open_nav > 0 else 0)))
            if int(rec["trades"]) != len(fills): violations["daily_trade_count"] += 1
            if int(rec["positions"]) != sum(q > 1e-9 for q in units.values()): violations["daily_position_count"] += 1
            if wind_days and truth(rec.get("wind_down", False)) != (date in wind_dates): violations["daily_wind_down_calendar"] += 1
            if cash < -MONEY_TOL or any(q < -1e-7 for q in units.values()): violations["negative_cash_or_position"] += 1
            participation = 0.
            for symbol, notional in used.items():
                lag, day_cap, amount = source.capacities(date, symbol, cfg)
                errors["combined_capacity_excess"] = max(errors["combined_capacity_excess"], max(0., notional - min(lag, day_cap)))
                participation = max(participation, notional / amount if amount else math.inf)
            if participation > cfg["max_daily_participation"] + 1e-10: violations["daily_participation"] += 1
            if "daily_capacity_max_participation" in rec:
                errors["participation_record"] = max(errors["participation_record"], abs(participation - rec["daily_capacity_max_participation"]))
            rejected = [o for o in order_days.get(date, []) if o["status"] == "REJECTED"]
            if int(rec["blocked_attempts"]) != len(rejected): violations["daily_blocked_count"] += 1
            for order in order_days.get(date, []):
                if not order["signal_date"] < date: violations["nonlagged_order_signal"] += 1
                if wind_days and truth(order.get("wind_down", False)) != (date in wind_dates): violations["order_wind_down_flag"] += 1
                if date in wind_dates and abs(order["target_adjusted_units"]) > 1e-9: violations["nonzero_wind_down_target"] += 1
                if order["status"] == "FILLED" and order["filled_notional"] > order["requested_notional"] + MONEY_TOL: violations["overfilled_order"] += 1
                if order["status"] == "FILLED" and order["filled_execution_notional"] > order["requested_execution_notional"] + MONEY_TOL: violations["overfilled_execution_amount"] += 1
                lag, cap, _ = source.capacities(date, order["symbol"], cfg)
                errors["order_capacity_lag"] = max(errors["order_capacity_lag"], abs(lag - order["capacity_lagged"]))
                errors["order_capacity_day"] = max(errors["order_capacity_day"], abs(cap - order["capacity_daily_posthoc"]))
            filled_orders = [o for o in order_days.get(date, []) if o["status"] == "FILLED"]
            if [(o["symbol"], o["side"]) for o in filled_orders] != [(t["symbol"], t["side"]) for t in fills]: violations["order_fill_pairing"] += 1
            errors["orders_filled_notional"] = max(errors["orders_filled_notional"], abs(math.fsum(o["filled_notional"] for o in filled_orders) - math.fsum(day_notional)))
            errors["orders_execution_notional"] = max(errors["orders_execution_notional"], abs(math.fsum(o["filled_execution_notional"] for o in filled_orders) - math.fsum(execution_amounts)))
            last_nav = nav
        observed_holdings = {r["symbol"]: r for r in holdings.to_dict("records")} if not holdings.empty else {}
        expected_holdings = {s: q for s, q in units.items() if q > 1e-9}
        if set(observed_holdings) != set(expected_holdings): violations["terminal_holdings_symbols"] += 1
        for symbol, q in expected_holdings.items():
            r = observed_holdings.get(symbol)
            if r:
                errors["terminal_units"] = max(errors["terminal_units"], abs(q - r["adjusted_units"]))
                errors["terminal_holding_value"] = max(errors["terminal_holding_value"], abs(q * marks[symbol] - r["value"]))
        terminal_value = math.fsum(q * marks[s] for s, q in expected_holdings.items())
        errors["terminal_summary_value"] = abs(terminal_value - row["terminal_position_value"])
        errors["terminal_diag_value"] = abs(terminal_value - diag["terminal"]["residual_value"])
        terminal = diag["terminal"]
        if terminal["residual_positions"] != len(expected_holdings) or terminal["fully_liquidated"] != (len(expected_holdings) == 0): violations["terminal_liquidation_claim"] += 1
        rejected = orders.loc[orders.status == "REJECTED"] if not orders.empty else pd.DataFrame()
        counts = dict(Counter(rejected.reason)) if not rejected.empty else {}
        if {k: int(v) for k, v in diag["blocked"].items() if v} != counts: violations["blocked_reason_reconciliation"] += 1
        terminal_counts = Counter(r["reason"] for r in orders.to_dict("records") if r["status"] == "REJECTED" and truth(r.get("terminal", False)))
        terminal_used = defaultdict(float)
        for order in orders.to_dict("records"):
            if not truth(order.get("terminal", False)) or order["status"] != "FILLED": continue
            symbol = order["symbol"]
            lag, cap, _ = source.capacities(order["date"], symbol, cfg)
            available = max(0., min(lag, cap) - terminal_used[symbol])
            if order["reason"] == "partial":
                terminal_counts["partial_capacity_remaining" if order["requested_execution_notional"] > available + 1e-7 else "partial_lot_remaining"] += 1
            terminal_used[symbol] += order["filled_execution_notional"]
        if dict(terminal_counts) != terminal["blocked_reasons"]: violations["terminal_blocked_reconciliation"] += 1
        if wind_days:
            if terminal.get("wind_down_start") != sorted(wind_dates)[0] or terminal.get("wind_down_days") != len(wind_dates) or terminal.get("wind_down_days_requested") != wind_days:
                violations["wind_down_protocol_calendar"] += 1
            wind_counts = Counter(r["reason"] for r in orders.to_dict("records") if r["status"] == "REJECTED" and r["date"] in wind_dates)
            wind_used = defaultdict(float)
            for order in orders.to_dict("records"):
                if order["date"] not in wind_dates or order["status"] != "FILLED": continue
                k = (order["date"], order["symbol"])
                lag, cap, _ = source.capacities(order["date"], order["symbol"], cfg)
                available = max(0., min(lag, cap) - wind_used[k])
                if order["reason"] == "partial": wind_counts["partial_capacity_remaining" if order["requested_execution_notional"] > available + 1e-7 else "partial_lot_remaining"] += 1
                wind_used[k] += order["filled_execution_notional"]
            if dict(wind_counts) != terminal.get("wind_down_rejections"): violations["wind_down_rejection_reconciliation"] += 1
        if terminal["date"] != daily.date.iloc[-1] or not truth(daily.terminal_liquidation.iloc[-1]): violations["terminal_date_or_attempt"] += 1
        if sum(diag["blocked"].values()) != row["blocked_attempts"]: violations["summary_blocked_count"] += 1
        if diag["execution_diagnostics"].get("historical_st_known_observations") != 0: violations["invented_historical_ST_truth"] += 1
        money_errors = {k: v for k, v in errors.items() if k not in {"daily_return", "daily_turnover", "participation_record", "raw_share_from_adjusted_units", "terminal_units"}}
        passed = not violations and all(math.isfinite(v) and v <= MONEY_TOL for v in money_errors.values()) and errors["daily_return"] < 1e-10 and errors["daily_turnover"] < 1e-10 and errors["participation_record"] < 1e-10 and errors["raw_share_from_adjusted_units"] < 1e-6 and errors["terminal_units"] < 1e-7
        report = {"key": list(key), "passed": passed, "daily_rows": len(daily), "ledger_rows": len(trades), "orders": len(orders), "errors": dict(errors), "violations": dict(violations), "terminal_positions": len(expected_holdings), "terminal_value": terminal_value}
        self.ledger_reports.append(report)
        self.check("independent_ledger:" + "/".join(key), passed, violations=dict(violations), max_money_error=max(money_errors.values(), default=0.))

    def run(self):
        study, protocol = load(self.out / "result.json"), load(self.out / "protocol.json")
        self.study = study
        self.check("protocol_matches_result_and_is_retrospective_zip_only", protocol == study["protocol"] and protocol["retrospective"] is True and protocol["true_ST_coverage"] == 0)
        grid = protocol["grid"]
        ids = [r["experiment_id"] for r in grid]
        full_grid = {f"{f}__{u}__n{n}__{frequency}" for f in FACTORS for u in ("all_liquid", "main_liquid", "liquid1000") for n in (50, 100) for frequency in ("weekly", "monthly")}
        self.check("full_120_candidate_grid_or_explicit_development_only", len(ids) == len(set(ids)) and ((set(ids) == full_grid and not protocol["quick_development_run"]) or (self.allow_quick and protocol["quick_development_run"])))
        self.check("declared_periods_match_exact_study_scope", {k: tuple(v) for k, v in protocol["periods"].items()} == PERIODS)
        selection, experiments = study["selection"], study["experiments"]
        keys = [(r["experiment_id"], r["period_role"], r["scenario"]) for r in experiments]
        expected = {(eid, role, "base") for eid in ids for role in ("selection_2024", "evaluation_2025")}
        expected |= {(eid, "evaluation_2025", scenario) for eid in selection["shortlist"] for scenario in ("gross", "stress", "fees_only", "capacity_tight")}
        expected |= {(eid, "assignment_2025_2026sep", "base") for eid in selection["shortlist"]}
        self.check("all_declared_candidates_intervals_and_scenarios_present", len(keys) == len(set(keys)) and set(keys) == expected, missing=[list(k) for k in sorted(expected - set(keys))], unexpected=[list(k) for k in sorted(set(keys) - expected)])
        self.check("no_hidden_or_ignored_failed_experiments", study["status"] == "complete" and not study["failures"])
        frozen = load(self.out / "selection_frozen_before_2025.json")
        self.check("selected_ids_equal_frozen_pre_2025_document", all(frozen.get(k) == v for k, v in selection.items()))
        detailed_ids = set(selection["shortlist"]) | set(selection["factor_representatives"].values())
        detailed_rows = [r for r in experiments if r["period_role"] != "selection_2024" and (r["experiment_id"] in detailed_ids and r["scenario"] == "base" or r["experiment_id"] in selection["shortlist"])]
        symbols = set()
        for row in detailed_rows:
            folder = self.out / "runs" / row["experiment_id"] / row["period_role"] / row["scenario"]
            present = all((folder / (name + ".csv")).is_file() for name in ("daily", "trades", "holdings", "orders"))
            self.check("full_ledger_files:" + "/".join((row["experiment_id"], row["period_role"], row["scenario"])), present)
            if present:
                for name in ("trades", "holdings", "orders"):
                    f = frame(folder / (name + ".csv"))
                    if "symbol" in f: symbols.update(f.symbol.dropna().astype(str))
        source = Source(self.source, symbols)
        self.check("source_2025_has_243_dates_and_requested_boundaries", len([d for d in source.dates if "2025-01-02" <= d <= "2025-12-31"]) == 243 and "2025-01-02" in source.date_index and "2025-12-31" in source.date_index)
        metric_errors, calendar_failures, config_failures = [], [], []
        for row in experiments:
            key = (row["experiment_id"], row["period_role"], row["scenario"])
            daily = frame(self.out / "runs" / key[0] / key[1] / key[2] / "daily.csv")
            start, end = PERIODS[key[1]]
            dates = [d for d in source.dates if start <= d <= end]
            if daily.date.tolist() != dates or row["days"] != len(dates): calendar_failures.append(list(key))
            cfg = row["config"]
            if (cfg["start"], cfg["end"]) != (start, end) or cfg["capital"] != 1_000_000_000 or cfg["st_policy"] != "price_proxy" or cfg["execution_model"] != "daily_conservative" or not cfg["liquidate_end"]: config_failures.append(list(key))
            base = protocol["base_config"]
            expected_slip = 0. if key[2] == "fees_only" else base["slippage"]
            expected_part = .001 if key[2] == "capacity_tight" else base["max_participation"]
            expected_day_part = .001 if key[2] == "capacity_tight" else base["max_daily_participation"]
            if cfg["slippage"] != expected_slip or cfg["max_participation"] != expected_part or cfg["max_daily_participation"] != expected_day_part or cfg["commission"] != base["commission"] or cfg["min_commission"] != base["min_commission"]: config_failures.append([*key, "scenario_fee_or_capacity"])
            calculated = metric_values(daily, cfg["capital"])
            self.metric_cache[key] = calculated
            for name, value in calculated.items():
                if abs(value - row[name]) > (MONEY_TOL if name in {"costs", "writeoff_loss", "final_nav"} else 1e-9): metric_errors.append({"key": list(key), "field": name, "difference": float(value - row[name])})
        self.check("every_run_uses_all_and_only_its_source_calendar_dates", not calendar_failures, failures=calendar_failures)
        self.check("every_run_config_matches_capital_period_proxy_and_liquidation_protocol", not config_failures, failures=config_failures)
        self.check("reported_metrics_recomputed_from_every_persisted_daily_nav", not metric_errors, failures=metric_errors[:30])
        train = [{**r, **self.metric_cache[(r["experiment_id"], r["period_role"], r["scenario"]) ]} for r in experiments if r["period_role"] == "selection_2024" and r["scenario"] == "base"]
        safe = [r for r in train if r["mean_invested"] >= .25 and r["max_drawdown"] >= -.40 and r["writeoff_loss"] == 0]
        ranked = sorted(safe or train, key=lambda r: (-r["sharpe"], -r["total_return"], r["experiment_id"]))
        self.check("champion_reconstructed_only_from_2024_daily_results", selection["selected_on"] == "selection_2024" and selection["selected_id"] == ranked[0]["experiment_id"] and selection["safety_screen_fallback"] == (not bool(safe)))
        self.check("factor_representatives_cover_all_protocol_factors", set(selection["factor_representatives"]) == {r["factor"] for r in grid})
        reps, fallbacks = {}, {}
        for factor in {r["factor"] for r in grid}:
            all_factor = [r for r in train if r["factor"] == factor]
            safe_factor = [r for r in safe if r["factor"] == factor]
            reps[factor] = sorted(safe_factor or all_factor, key=lambda r: (-r["sharpe"], -r["total_return"], r["experiment_id"]))[0]["experiment_id"]
            fallbacks[factor] = not bool(safe_factor)
        self.check("each_factor_representative_and_fallback_use_2024_only", reps == selection["factor_representatives"] and fallbacks == selection.get("factor_safety_screen_fallback"))
        shortlist = [r["experiment_id"] for r in ranked[:3]]
        for factor in ("dr5_momentum", "dr5_reversal", "lowvol20"):
            if factor in reps and reps[factor] not in shortlist: shortlist.append(reps[factor])
        self.check("shortlist_has_no_2025_selection_or_hidden_replacement", shortlist == selection["shortlist"])
        selected_row = next(r for r in experiments if r["experiment_id"] == selection["selected_id"] and r["period_role"] == "evaluation_2025" and r["scenario"] == "base")
        selected_cfg, selected_doc = load(self.out / "selected_config.json"), load(self.out / "selected_strategy.json")
        factor_doc = load(self.out / "strategies" / (selected_row["factor"] + ".json"))
        self.check("published_selected_strategy_and_config_match_selected_experiment", selected_cfg == selected_row["config"] and selected_doc["name"] == selected_row["name"] and selected_doc["score"] == factor_doc["score"] and selected_doc["selection"]["top_n"] == selected_cfg["top_n"] and selected_doc["selection"]["rebalance"] == selected_cfg["rebalance"])
        self.check("all_experiment_diagnostics_disclose_unknown_ST_and_unverified_opening_liquidity", all(r["execution_diagnostics"].get("historical_st_known_observations") == 0 and r["execution_diagnostics"].get("opening_liquidity_verified") is False and r["execution_diagnostics"].get("posthoc_not_opening_information") is True for r in experiments))
        self.check("shortlist_IC_coverage_and_only_raw_P_inference", {r["experiment_id"] for r in study.get("ic", [])} == set(selection["shortlist"]) and all("p_raw_hac_normal" in r and r.get("hac_lags") == 5 for r in study.get("ic", [])))
        expected_uncertainty = {r["experiment_id"] for r in experiments if r["period_role"] == "evaluation_2025" and r["scenario"] == "base" and r["experiment_id"] in detailed_ids}
        self.check("fixed_portfolio_uncertainty_covers_detailed_base_only", {r["experiment_id"] for r in study.get("uncertainty", [])} == expected_uncertainty and all(r["repetitions"] == 2000 and r["block_length"] == 10 and r["seed"] == 20251016 and r["return_ci95_low"] <= r["return_ci95_high"] for r in study.get("uncertainty", [])))
        frozen_time = datetime.fromisoformat(frozen["frozen_at"]).timestamp()
        eval_paths = list((self.out / "runs").glob("*/evaluation_2025/*/daily.csv"))
        self.check("selection_artifact_precedes_2025_result_files", all(p.stat().st_mtime + 1 >= frozen_time for p in eval_paths))
        for row in detailed_rows:
            try:
                self.detail(row, source)
            except Exception as exc:
                self.check("independent_ledger:" + "/".join((row["experiment_id"], row["period_role"], row["scenario"])), False, error=str(exc))
        self.check("all_required_detailed_ledgers_were_reconstructed", len(self.ledger_reports) == len(detailed_rows))


class RepairAudit(Audit):
    """Audit additional posthoc exit cases without reinterpreting the 265 baseline."""
    def __init__(self, baseline, directory, source):
        super().__init__(directory, source, False)
        self.baseline = baseline

    def run(self):
        original = load(self.baseline / "result.json")
        original_check = load(self.baseline / "validation.json")
        repair = load(self.out / "feasibility_repair.json")
        self.study = repair
        short = original["selection"]["shortlist"]
        self.check("baseline_265_preserved_and_previously_independently_passed", len(original["experiments"]) == 265 and original_check["status"] == "passed")
        self.check("repair_explicitly_posthoc_and_keeps_original_2024_selection", repair["posthoc"] is True and repair["study_id"] == original["study_id"] and repair["selected_id"] == original["selection"]["selected_id"] and repair["original_shortlist"] == short)
        self.check("repair_complete_without_suppressed_failures", repair["status"] == "complete" and not repair["failures"])
        windows = repair.get("windows", repair["protocol"].get("wind_down_windows", repair["protocol"].get("windows")))
        self.check("only_predeclared_20_and_40_date_exit_windows", windows == [20, 40])
        experiments = repair["experiments"]
        expected = {(eid, role, scenario) for eid in short for role in ("evaluation_2025", "assignment_2025_2026sep") for scenario in ("wind_down20", "wind_down40")}
        expected |= {(eid, "evaluation_2025", scenario) for eid in short for scenario in ("wind_down20_stress", "wind_down20_capacity_tight", "wind_down40_capacity_tight")}
        keys = [(r["experiment_id"], r["period_role"], r["scenario"]) for r in experiments]
        self.check("all_35_additional_scenarios_without_changing_frozen_baseline", len(keys) == 35 and len(set(keys)) == 35 and set(keys) == expected,
                   missing=[list(x) for x in sorted(expected - set(keys))], unexpected=[list(x) for x in sorted(set(keys) - expected)])
        symbols, missing = set(), []
        for row in experiments:
            folder = self.out / row["folder"]
            folder.resolve().relative_to(self.out.resolve())
            for name in ("daily", "trades", "orders", "holdings"):
                path = folder / (name + ".csv")
                if not path.is_file(): missing.append(str(path.relative_to(self.out))); continue
                if name != "daily":
                    f = frame(path)
                    if "symbol" in f: symbols.update(f.symbol.dropna().astype(str))
        self.check("every_repair_case_has_full_reconstructable_ledger", not missing, missing=missing)
        source = Source(self.source, symbols)
        issues = []
        original_rows = {(r["experiment_id"], r["period_role"]): r for r in original["experiments"] if r["scenario"] == "base"}
        freeze_time = datetime.fromisoformat(repair["protocol"]["frozen_at"]).timestamp()
        for row in experiments:
            key = (row["experiment_id"], row["period_role"], row["scenario"])
            cfg, scenario = row["config"], row["scenario"]
            original_cfg = original_rows[key[:2]]["config"]
            expected_cfg = {**original_cfg, "wind_down_days": 40 if scenario.startswith("wind_down40") else 20}
            if scenario.endswith("capacity_tight"):
                expected_cfg.update(max_participation=.001, max_daily_participation=.001)
            expected_mult = 2. if scenario.endswith("stress") else 1.
            if cfg != expected_cfg or row.get("cost_multiplier") != expected_mult: issues.append({"key": list(key), "issue": "undeclared_config_or_cost_multiplier_change"})
            path = self.out / row["folder"] / "daily.csv"
            daily = frame(path)
            dates = [d for d in source.dates if PERIODS[key[1]][0] <= d <= PERIODS[key[1]][1]]
            if daily.date.tolist() != dates or row["days"] != len(dates): issues.append({"key": list(key), "issue": "calendar_mismatch"})
            if path.stat().st_mtime + 1 < freeze_time: issues.append({"key": list(key), "issue": "repair_protocol_frozen_after_case"})
            for name, value in metric_values(daily, cfg["capital"]).items():
                if abs(value - row[name]) > (MONEY_TOL if name in {"costs", "writeoff_loss", "final_nav"} else 1e-9): issues.append({"key": list(key), "issue": "metric_mismatch", "field": name})
            try:
                self.detail(row, source)
            except Exception as exc:
                self.check("independent_ledger:" + "/".join(key), False, error=str(exc))
        self.check("repair_configs_multipliers_calendars_metrics_and_freeze_match", not issues, failures=issues[:40])
        self.check("all_35_repair_cash_and_NAV_ledgers_reconstructed", len(self.ledger_reports) == len(experiments))
        independent = {tuple(r["key"]): r for r in self.ledger_reports}
        choices = repair["selection_by_liquidation_only"]
        pairs = [(r["experiment_id"], r["period_role"]) for r in choices]
        expected_pairs = {(eid, role) for eid in short for role in ("evaluation_2025", "assignment_2025_2026sep")}
        selection_errors = []
        for choice in choices:
            pair = (choice["experiment_id"], choice["period_role"])
            selected = None
            for scenario, days in (("wind_down20", 20), ("wind_down40", 40)):
                observed = independent.get((*pair, scenario))
                if observed and observed["terminal_positions"] == 0 and observed["terminal_value"] <= MONEY_TOL:
                    selected = (scenario, days)
                    break
            if (choice["scenario"], choice["wind_down_days"]) != (selected or (None, None)) or choice["fully_liquidated"] != (selected is not None) or not choice["reason"]:
                selection_errors.append(list(pair))
        self.check("exit_choice_uses_20_then_40_days_zero_positions_not_2025_profit", len(pairs) == 10 and len(set(pairs)) == 10 and set(pairs) == expected_pairs and not selection_errors, failures=selection_errors)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("study", type=Path)
    p.add_argument("--source", type=Path, default=PROJECT.parent / "feathers.zip")
    p.add_argument("--allow-quick", action="store_true")
    p.add_argument("--repair", action="store_true", help="Audit posthoc feasibility_repair separately; never overwrite baseline validation")
    p.add_argument("--repair-dir", type=Path)
    args = p.parse_args()
    directory = args.study if args.study.is_dir() else args.study.parent
    audit = RepairAudit(directory, args.repair_dir or directory / "feasibility_repair", args.source) if args.repair else Audit(directory, args.source, args.allow_quick)
    error = None
    try:
        audit.run()
    except Exception:
        error = traceback.format_exc()
    passed = sum(c["passed"] for c in audit.checks)
    report = {"status": "passed" if audit.checks and passed == len(audit.checks) and error is None else "failed", "passed": passed, "total": len(audit.checks),
              "checks": audit.checks, "detailed_ledgers": audit.ledger_reports, "error": error, "money_absolute_tolerance": MONEY_TOL,
              "source": str(args.source), "does_not_import_research_or_simulator": True,
              "scope": "All result daily calendars and metrics; full signed-cash and original-price NAV reconstruction only for persisted detailed ledgers; no ST or auction truth available"}
    report_path = audit.out / ("feasibility_validation.json" if args.repair else "validation.json")
    if args.repair:
        report.update(posthoc_repair=True, baseline_validation_untouched=True, scope="All 35 posthoc repair cases, full cash/price NAV reconstruction, window-only feasibility selection; no proof of auction fills or real historical ST")
    report_path.write_text(json.dumps(safe_json(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "passed", "total", "error")}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
