"""Independent persisted-file V4 audit. No simulator/factor-engine imports.

All primary daily paths are recomputed; full source-price ledger reconstruction
is explicitly limited to the frozen detailed sample. Money tolerance is .001.
"""
import os
for _key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_key] = "1"

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path
import traceback
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather

pa.set_cpu_count(1)
pa.set_io_thread_count(1)
ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "feathers.zip"
MONEY_TOL = .001
RATIO_TOL = 1e-10
PRIMARY = (("selection_2024", "base"), ("evaluation_2025", "base"),
           ("evaluation_2025", "stress"), ("evaluation_2025", "capacity"))
REQUIRED_CODE = {"lab.py", "execution_metadata.py", "research.py", "strategy_blocks.py", "v4_factors.py", "research_v4.py"}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def json_arrays(value):
    """Match JSON's tuple-to-array representation without altering scalars.

    Protocol constructors retain tuple periods in memory; JSON reloads lists.
    Keys, sequence order, missing fields, and numeric precision stay unchanged.
    """
    if isinstance(value, dict): return {k: json_arrays(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [json_arrays(v) for v in value]
    return value


def frame(path):
    return pd.read_csv(path, dtype={"date": str, "signal_date": str, "symbol": str}, float_precision="round_trip")


def truth(value):
    return value is True or str(value).lower() in ("true", "1")


def clean(value):
    if isinstance(value, dict): return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)): return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer): return int(value)
    if isinstance(value, np.bool_): return bool(value)
    return value


def key(row):
    return "|".join(str(row.get(k, "MISSING")) for k in ("experiment_id", "period_role", "scenario"))


def difference(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.shape != b.shape or not np.isfinite(a).all() or not np.isfinite(b).all(): return math.inf
    return float(np.max(np.abs(a - b), initial=0.))


def metrics_from_daily(daily, capital):
    nav = daily.nav.to_numpy(float)
    previous = np.r_[capital, nav[:-1]]
    returns = np.divide(nav, previous, out=np.ones_like(nav), where=previous > 0) - 1.
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.
    peak = np.maximum.accumulate(np.r_[capital, nav])[1:]
    result = {"days": len(nav), "final_nav": float(nav[-1]), "total_return": float(nav[-1] / capital - 1.),
              "annual_return": float((nav[-1] / capital) ** (252 / len(nav)) - 1) if nav[-1] > 0 else -1.,
              "annual_vol": std * math.sqrt(252), "sharpe": float(returns.mean() / std * math.sqrt(252)) if std else 0.,
              "max_drawdown": float(np.min(nav / peak - 1.)), "costs": math.fsum(daily.cost),
              "mean_invested": float(daily.invested_fraction.mean()), "trades": int(daily.trades.sum()),
              "writeoff_loss": math.fsum(daily.writeoff_loss), "max_stale_fraction": float(daily.stale_fraction.max()),
              "annual_two_sided_turnover": math.fsum(daily.turnover) * 252 / len(daily),
              "blocked_attempts": int(daily.blocked_attempts.sum()), "terminal_positions": int(daily.positions.iloc[-1]),
              "terminal_position_value": float(daily.nav.iloc[-1] - daily.cash.iloc[-1])}
    result["book_position_zero"] = result["terminal_positions"] == 0
    result["exit_by_trades"] = result["book_position_zero"] and result["terminal_position_value"] <= MONEY_TOL and result["writeoff_loss"] == 0.
    quarters, halves = {}, {}
    for period, indexes in daily.groupby(pd.to_datetime(daily.date).dt.to_period("Q")).groups.items():
        ids = np.asarray(list(indexes), dtype=int)
        quarters[str(period)] = float(nav[ids[-1]] / previous[ids[0]] - 1.) if previous[ids[0]] > 0 else 0.
    for label, mask in (("H1", daily.date.str[5:7] <= "06"), ("H2", daily.date.str[5:7] >= "07")):
        ids = np.flatnonzero(mask)
        if len(ids): halves[label] = float(nav[ids[-1]] / previous[ids[0]] - 1.) if previous[ids[0]] > 0 else 0.
    result.update(quarter_returns=quarters, half_returns=halves)
    return result, returns


class Source:
    """Independent raw ZIP reader, bounded to symbols used in detailed ledgers."""
    def __init__(self, symbols=(), source=SOURCE):
        self.symbols, self.raw = sorted(set(symbols)), {}
        self.symbol_index = {s: i for i, s in enumerate(self.symbols)}
        with zipfile.ZipFile(source) as archive:
            for field in ("close", "open", "high", "low", "amt", "cumadj"):
                table = feather.read_table(pa.BufferReader(archive.read("feathers/" + field + ".feather")), columns=["date"] + self.symbols)
                dates = np.asarray(table["date"].to_numpy()).astype("datetime64[D]").astype(str)
                if not self.raw:
                    self.dates = dates
                    self.date_index = {d: i for i, d in enumerate(dates)}
                elif not np.array_equal(dates, self.dates): raise ValueError("ZIP date axes differ")
                self.raw[field] = np.column_stack([table[s].to_numpy() for s in self.symbols]) if self.symbols else np.empty((len(dates), 0))
        factors = self.raw["cumadj"]
        valid = np.isfinite(factors) & (factors > 0)
        first = valid.argmax(axis=0)
        self.base = factors[first, np.arange(len(self.symbols))]

    def price(self, field, date, symbol):
        return float(self.raw[field][self.date_index[date], self.symbol_index[symbol]])

    def scale(self, date, symbol):
        return self.price("cumadj", date, symbol) / self.base[self.symbol_index[symbol]]

    def capacities(self, date, symbol, cfg):
        i, j = self.date_index[date], self.symbol_index[symbol]
        window = self.raw["amt"][max(0, i-20):i, j]
        lag = float(window.mean()) if len(window) == 20 and np.isfinite(window).all() else 0.
        current = self.raw["amt"][i, j]
        current = max(0., float(current)) if math.isfinite(current) else 0.
        return max(0., lag * cfg["max_participation"]), current * cfg["max_daily_participation"], current


def _detail(out, row, source):
    cfg, run_key = row["config"], key(row)
    folder = (out / row["folder"]).resolve()
    folder.relative_to(out.resolve())
    daily, ledger, holdings, orders = [frame(folder / (name + ".csv")) for name in ("daily", "trades", "holdings", "orders")]
    diag = load(folder / "diagnostics.json")
    errors, violations = defaultdict(float), Counter()
    ratio_errors = {"return", "turnover", "participation", "invested", "stale_fraction", "holding_weight", "tick_fill", "source_open"}
    quantity_errors = {"raw_equivalent", "holding_units"}
    def err(name, a, b): errors[name] = max(errors[name], difference(a, b))
    cash = last_nav = float(cfg["capital"])
    units, marks, stale, bought = defaultdict(float), defaultdict(float), defaultdict(int), {}
    days = list(daily.date)
    wind = set(days[-min(int(cfg.get("wind_down_days", 0)), len(days)):]) if cfg.get("wind_down_days", 0) else set()
    terminal_requested = bool(cfg.get("liquidate_end") or wind)
    terminal_date = days[-1]
    by_trade = {d: v.to_dict("records") for d, v in ledger.groupby("date", sort=False)}
    by_order = {d: v.to_dict("records") for d, v in orders.groupby("date", sort=False)}
    if not ledger.date.isin(days).all() or not orders.date.isin(days).all(): violations["ledger_date_outside_calendar"] += 1
    if not pd.to_datetime(ledger.date).is_monotonic_increasing: violations["unsorted_ledger"] += 1
    multiplier = float(row["cost_multiplier"])
    total_writeoff = 0.
    terminal_reasons, wind_reasons, global_reasons = Counter(), Counter(), Counter()
    if load(folder/"record.json")!=row:violations["persisted_detail_record"]+=1
    expected_dates=source.dates[(source.dates>=cfg["start"])&(source.dates<=cfg["end"])&(np.arange(len(source.dates))>0)]
    if list(daily.date)!=list(expected_dates):violations["detail_calendar"]+=1
    computed,_=metrics_from_daily(daily,cfg["capital"])
    for field,value in computed.items():
        if isinstance(value,dict):
            if set(value)!=set(row["metrics"].get(field,{})) or any(difference(v,row["metrics"][field][k])>RATIO_TOL for k,v in value.items()):violations["detail_metric_"+field]+=1
        elif isinstance(value,bool):
            if row["metrics"].get(field) is not value:violations["detail_metric_"+field]+=1
        elif difference(value,row["metrics"].get(field))>(MONEY_TOL if field in {"final_nav","costs","writeoff_loss","terminal_position_value"} else RATIO_TOL):violations["detail_metric_"+field]+=1
    if diag["terminal"]!=row["terminal"] or diag["blocked"]!=row["blocked"] or diag["execution_diagnostics"]!=row["execution_diagnostics"]:violations["detail_diagnostics_record"]+=1
    for day in daily.to_dict("records"):
        date = day["date"]
        def mark(symbol, field):
            value = source.price(field, date, symbol) * source.scale(date, symbol)
            return value if math.isfinite(value) and value > 0 else None
        open_nav = cash + math.fsum(q * (mark(s, "open") or marks[s]) for s, q in units.items() if q > 1e-9)
        used, fees, exec_amounts, reference_amounts, writeoffs = defaultdict(float), [], [], [], []
        fills, expired = [], []
        for trade in by_trade.get(date, []):
            side, symbol = trade["side"], trade["symbol"]
            if side == "WRITE_OFF":
                expired.append(trade)
                err("writeoff_cash", cash, trade["cash_after"])
                if trade.get("total", 0) != 0: violations["writeoff_fake_fee"] += 1
                continue
            if side not in ("BUY", "SELL"): violations["unknown_side"] += 1; continue
            q, raw_q, fill = float(trade["adjusted_units"]), float(trade["raw_share_equivalent"]), float(trade["fill_price_raw"])
            opening = source.price("open", date, symbol)
            actual = raw_q * fill
            if not all(math.isfinite(v) and v > 0 for v in (q, raw_q, fill, opening)): violations["invalid_fill"] += 1
            if not str(trade["signal_date"]) < date: violations["trade_future_signal"] += 1
            if side == "SELL" and (q > units[symbol] + 1e-7 or bought.get(symbol) == date): violations["T1_or_oversell"] += 1
            if side == "BUY" and (date in wind or (terminal_requested and date == terminal_date)): violations["buy_during_exit"] += 1
            if truth(trade.get("terminal")) != (terminal_requested and date == terminal_date): violations["trade_terminal_flag"] += 1
            if truth(trade.get("wind_down")) != (date in wind): violations["trade_wind_flag"] += 1
            if side == "BUY":
                star = symbol.startswith(("688", "689")) and symbol.endswith(".SH")
                if star and (raw_q < 200 - 1e-7 or abs(raw_q-round(raw_q)) > 1e-6): violations["star_lot"] += 1
                if not star and (raw_q < 100 - 1e-7 or abs(raw_q/100-round(raw_q/100)) > 1e-8): violations["main_growth_lot"] += 1
            impacted = opening * (1 + (1 if side == "BUY" else -1) * cfg["slippage"] * multiplier)
            tick = (math.ceil(impacted * 100 - 1e-8) if side == "BUY" else math.floor(impacted * 100 + 1e-8)) / 100
            err("tick_fill", tick, fill)
            err("source_open", opening, trade["open_raw"])
            err("reference_amount", opening*raw_q, trade["notional"])
            err("execution_amount", actual, trade["execution_notional"])
            err("raw_equivalent", q * source.scale(date, symbol), raw_q)
            i = source.date_index[date]
            prior_factor = source.price("cumadj", source.dates[i-1], symbol)
            factor = source.price("cumadj", date, symbol)
            if not math.isfinite(prior_factor) or prior_factor <= 0 or abs(factor/prior_factor - 1) > 1e-8: violations["factor_action_reference"] += 1
            prior=source.price("close",source.dates[i-1],symbol)
            if not math.isfinite(prior) or prior<=0:violations["invalid_price_reference"]+=1
            else:
                growth=symbol.startswith("30") and symbol.endswith(".SZ")
                star=symbol.startswith(("688","689")) and symbol.endswith(".SH")
                main=(symbol.startswith("6") and symbol.endswith(".SH") and not star) or (symbol.startswith("00") and symbol.endswith(".SZ"))
                if not (main or growth or star):violations["unknown_board_fill"]+=1
                rate=.2 if (growth or star) else .1
                half_up=lambda x:math.floor(x*100+.5+1e-9)/100
                upper,lower=half_up(prior*(1+rate)),half_up(prior*(1-rate))
                if side=="BUY" and (opening>=upper-.0049 or fill>=upper-.0049 or opening/prior-1>=cfg["open_guard"]):violations["upper_guard_fill"]+=1
                if side=="SELL" and (opening<=lower+.0049 or fill<=lower+.0049 or opening/prior-1<=-cfg["open_guard"]):violations["lower_guard_fill"]+=1
                if main and side=="BUY" and max(opening,fill)>=half_up(prior*1.05)-.0049:violations["unknown_ST_upper_fill"]+=1
                if main and side=="SELL" and min(opening,fill)<=half_up(prior*.95)+.0049:violations["unknown_ST_lower_fill"]+=1
            if fill > source.price("high", date, symbol) + .0049 or fill < source.price("low", date, symbol) - .0049: violations["outside_source_range"] += 1
            expected = {"commission": max(cfg["min_commission"], cfg["commission"] * actual) * multiplier,
                        "stamp": actual * (.001 if date < "2023-08-28" else .0005) * multiplier if side == "SELL" else 0.,
                        "transfer": actual * (.00002 if date < "2022-04-29" else .00001) * multiplier,
                        "slippage": abs(fill-opening) * raw_q}
            for name, value in expected.items(): err("fee_"+name, value, trade[name])
            err("fee_total", math.fsum(expected.values()), trade["total"])
            cash_fees = expected["commission"] + expected["stamp"] + expected["transfer"]
            cash += actual-cash_fees if side == "SELL" else -actual-cash_fees
            units[symbol] += -q if side == "SELL" else q
            if side == "BUY": bought[symbol] = date
            err("trade_cash", cash, trade["cash_after"])
            used[symbol] += actual
            exec_amounts.append(actual); reference_amounts.append(opening*raw_q); fees.append(math.fsum(expected.values())); fills.append(trade)
        for symbol, quantity in units.items():
            if quantity > 1e-9:
                closing, opening = mark(symbol, "close"), mark(symbol, "open")
                stale[symbol] = 0 if closing is not None else stale[symbol] + 1
                marks[symbol] = closing or opening or marks[symbol]
        for trade in expired:
            symbol = trade["symbol"]
            if stale[symbol] < cfg["writeoff_missing_days"]: violations["premature_writeoff"] += 1
            loss = units[symbol] * marks[symbol]
            err("writeoff_value", loss, trade["notional"])
            units[symbol] = 0.; writeoffs.append(loss)
        total_writeoff += math.fsum(writeoffs)
        nav = cash + math.fsum(q * marks[s] for s, q in units.items() if q > 1e-9)
        for name, value in (("cash", cash), ("nav", nav), ("cost", math.fsum(fees)), ("writeoff_loss", math.fsum(writeoffs)), ("daily_capacity_used", math.fsum(exec_amounts))):
            err("daily_"+name, value, day[name])
        err("return", nav/last_nav-1 if last_nav > 0 else 0., day["return"])
        err("turnover", math.fsum(exec_amounts)/open_nav if open_nav > 0 else 0., day["turnover"])
        err("invested", 1-cash/nav if nav else 0., day["invested_fraction"])
        stale_value = math.fsum(q*marks[s] for s,q in units.items() if q > 1e-9 and mark(s,"close") is None)
        err("stale_fraction", stale_value/nav if nav else 0., day["stale_fraction"])
        if int(day["positions"]) != sum(q > 1e-9 for q in units.values()): violations["position_count"] += 1
        if int(day["trades"]) != len(fills): violations["trade_count"] += 1
        if cash < -MONEY_TOL or any(q < -1e-7 for q in units.values()): violations["negative_cash_position"] += 1
        if truth(day["terminal_liquidation"]) != (terminal_requested and date == terminal_date): violations["daily_terminal_flag"] += 1
        if truth(day["wind_down"]) != (date in wind): violations["daily_wind_flag"] += 1
        participation = 0.
        for symbol, actual in used.items():
            lag, current_cap, amount = source.capacities(date, symbol, cfg)
            errors["capacity_excess"] = max(errors["capacity_excess"], max(0., actual-min(lag,current_cap)))
            participation = max(participation, actual/amount if amount else math.inf)
        err("participation", participation, day["daily_capacity_max_participation"])
        if participation > cfg["max_daily_participation"] + RATIO_TOL: violations["daily_participation"] += 1
        rejected, filled, order_used = [], [], defaultdict(float)
        for order in by_order.get(date, []):
            if not str(order["signal_date"]) < date: violations["order_future_signal"] += 1
            if truth(order.get("terminal")) != (terminal_requested and date == terminal_date): violations["order_terminal_flag"] += 1
            if truth(order.get("wind_down")) != (date in wind): violations["order_wind_flag"] += 1
            if (date in wind or (terminal_requested and date == terminal_date)) and abs(order["target_adjusted_units"]) > 1e-9: violations["exit_nonzero_target"] += 1
            lag, cap, _ = source.capacities(date, order["symbol"], cfg)
            err("order_lag_capacity", lag, order["capacity_lagged"]); err("order_daily_capacity", cap, order["capacity_daily_posthoc"])
            available = max(0., min(lag,cap) - order_used[order["symbol"]])
            reason = None
            if order["status"] == "REJECTED":
                rejected.append(order); global_reasons[order["reason"]] += 1; reason = order["reason"]
            elif order["status"] == "FILLED":
                filled.append(order)
                if order["filled_execution_notional"] > order["requested_execution_notional"] + MONEY_TOL: violations["overfilled_order"] += 1
                if order["reason"] == "partial": reason = "partial_capacity_remaining" if order["requested_execution_notional"] > available+1e-7 else "partial_lot_remaining"
                order_used[order["symbol"]] += order["filled_execution_notional"]
            else: violations["unknown_order_status"] += 1
            if reason and terminal_requested and date == terminal_date: terminal_reasons[reason] += 1
            if reason and date in wind: wind_reasons[reason] += 1
        if int(day["blocked_attempts"]) != len(rejected): violations["rejected_count"] += 1
        if [(x["symbol"],x["side"]) for x in filled] != [(x["symbol"],x["side"]) for x in fills]: violations["order_fill_pairing"] += 1
        err("order_fill_amount", math.fsum(x["filled_execution_notional"] for x in filled), math.fsum(exec_amounts))
        last_nav = nav
    final = {s:q for s,q in units.items() if q > 1e-9}
    observed = {r["symbol"]:r for r in holdings.to_dict("records")}
    if len(observed) != len(holdings) or set(final) != set(observed): violations["final_holding_symbols"] += 1
    for symbol,q in final.items():
        if symbol not in observed: continue
        err("holding_units", q, observed[symbol]["adjusted_units"])
        err("holding_value", q*marks[symbol], observed[symbol]["value"])
        err("holding_weight", q*marks[symbol]/last_nav if last_nav else 0., observed[symbol]["weight"])
    terminal = diag["terminal"]
    err("terminal_value", math.fsum(q*marks[s] for s,q in final.items()), terminal["residual_value"])
    err("terminal_writeoff", total_writeoff, terminal["writeoff_loss"])
    if terminal["residual_positions"] != len(final) or terminal["model_position_zero"] != (not final): violations["terminal_positions"] += 1
    if terminal["fully_exited_by_trades"] != (not final and total_writeoff == 0.): violations["trade_exit_claim"] += 1
    if terminal["fully_liquidated"] != (not final): violations["compatibility_zero_book_claim"] += 1
    if terminal["blocked_reasons"] != dict(terminal_reasons) or terminal["wind_down_rejections"] != dict(wind_reasons): violations["exit_rejection_reconciliation"] += 1
    if diag["blocked"] != dict(global_reasons): violations["all_rejection_reconciliation"] += 1
    money = {k:v for k,v in errors.items() if k not in ratio_errors | quantity_errors}
    passed = not violations and all(math.isfinite(v) and v <= MONEY_TOL for v in money.values()) and all(errors[k] <= RATIO_TOL for k in ratio_errors) and all(errors[k] <= 1e-6 for k in quantity_errors)
    return {"key": run_key, "passed": passed, "ledger_rows": len(ledger), "orders": len(orders), "daily_rows": len(daily),
            "max_money_error": max(money.values(),default=0.), "errors": dict(errors), "violations": dict(violations),
            "scope": "full independent cash/positions/NAV/tick fees/actual-fill turnover/orders/capacity reconstruction on declared detailed sample"}


def _validate_study(out, study, records):
    out = Path(out).resolve()
    checks, detail_reports, passed_keys, recomputed, primary_errors = [], [], [], {}, {}
    def check(name, passed, **evidence): checks.append({"name":name,"passed":bool(passed),**evidence})
    protocol = study["protocol"]
    grid = protocol["grid"]
    ids = [c["experiment_id"] for c in grid]
    expected = {"|".join((eid,role,scenario)) for eid in ids for role,scenario in PRIMARY}
    indexed = {key(r):r for r in records}
    check("candidate_grid_unique_and_count", len(ids)==len(set(ids))==protocol["candidate_count"] and len(study["candidates"])==len(ids) and {c["experiment_id"] for c in study["candidates"]}==set(ids))
    check("primary_exact_four_per_candidate", len(indexed)==len(records)==len(expected) and set(indexed)==expected and protocol["planned_primary_runs"]==len(expected), expected=len(expected), actual=len(records))
    check("protocol_money_tolerance_fixed", protocol.get("maximum_numeric_money_tolerance")==MONEY_TOL)
    check("retrospective_and_truth_disclosure", protocol.get("retrospective") is True and protocol.get("st_truth_available") is False and protocol.get("opening_auction_verified") is False)
    check("frozen_protocol_matches_disk", load(out/"protocol.json")==json_arrays(protocol))
    check("all_required_compute_hashes_frozen",REQUIRED_CODE<=set(protocol.get("code_sha256",{})))
    identity=protocol.get("source_identity",{})
    with SOURCE.open("rb") as stream:source_digest=hashlib.file_digest(stream,"sha256").hexdigest()
    check("frozen_source_identity",Path(identity.get("path","")).resolve()==SOURCE.resolve() and identity.get("size_bytes")==SOURCE.stat().st_size and identity.get("sha256")==source_digest)
    for name,digest in protocol.get("code_sha256",{}).items():
        path=(ROOT/name).resolve();path.relative_to(ROOT.resolve())
        check("frozen_code:"+name, path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==digest)
    # Load only the date column for primary checks; detailed source prices are loaded later.
    with zipfile.ZipFile(SOURCE) as archive:
        date_table=feather.read_table(pa.BufferReader(archive.read("feathers/close.feather")),columns=["date"])
    source_dates=np.asarray(date_table["date"].to_numpy()).astype("datetime64[D]").astype(str)
    check("source_calendar_unique_sorted", len(set(source_dates))==len(source_dates) and bool(np.all(source_dates[1:]>source_dates[:-1])))
    candidate_map={c["experiment_id"]:c for c in grid}
    evaluation_times=[];evaluation_starts=[];training_completions=[];all_starts=[]
    for run_key,row in indexed.items():
        problems=[]
        if row.get("status")!="success":
            check("primary:"+run_key,False,error=row.get("error","run failed"));primary_errors[run_key]=["simulation failed"];continue
        try:
            cfg=row["config"]; metrics=row["metrics"]; folder=(out/row["folder"]).resolve();folder.relative_to(out)
            persisted=load(folder/"record.json")
            if persisted!=row: problems.append("record.json differs from supplied record")
            daily=frame(folder/"daily.csv")
            expected_dates=source_dates[(source_dates>=cfg["start"])&(source_dates<=cfg["end"])&(np.arange(len(source_dates))>0)]
            if list(daily.date)!=list(expected_dates): problems.append("calendar coverage/order")
            numeric=daily[["nav","cash","return","cost","turnover","invested_fraction","writeoff_loss","stale_fraction","daily_capacity_used","daily_capacity_max_participation"]].to_numpy(float)
            if not np.isfinite(numeric).all(): problems.append("nonfinite daily values")
            if (daily.cash < -MONEY_TOL).any() or (daily.nav < -MONEY_TOL).any() or (daily.nav-daily.cash < -MONEY_TOL).any(): problems.append("negative cash/NAV/holdings")
            for col in ("cost","turnover","writeoff_loss","daily_capacity_used","daily_capacity_max_participation"):
                if (daily[col]<-RATIO_TOL).any(): problems.append("negative "+col)
            for col in ("positions","trades","blocked_attempts"):
                if not np.isfinite(daily[col]).all() or (daily[col]<0).any() or not np.equal(daily[col],np.floor(daily[col])).all(): problems.append("invalid integer "+col)
            computed,returns=metrics_from_daily(daily,float(cfg["capital"]));recomputed[run_key]=computed
            if difference(returns,daily["return"])>RATIO_TOL: problems.append("daily return from NAV")
            invested=np.divide(daily.cash,daily.nav,out=np.ones(len(daily)),where=daily.nav.to_numpy()>0)
            if difference(1-invested,daily.invested_fraction)>RATIO_TOL: problems.append("invested fraction from cash/NAV")
            for field,value in computed.items():
                tol=MONEY_TOL if field in {"final_nav","costs","writeoff_loss","terminal_position_value"} else RATIO_TOL
                if isinstance(value,dict):
                    if set(value)!=set(metrics.get(field,{})) or any(difference(v,metrics[field][k])>RATIO_TOL for k,v in value.items()): problems.append("metric "+field)
                elif isinstance(value,bool):
                    if metrics.get(field) is not value: problems.append("metric "+field)
                elif field not in metrics or difference(value,metrics[field])>tol: problems.append("metric "+field)
            cap=daily.daily_capacity_max_participation.max()
            diagnostic=row["execution_diagnostics"];terminal=row["terminal"]
            if cap>cfg["max_daily_participation"]+RATIO_TOL or diagnostic["max_combined_capacity_excess"]>MONEY_TOL or diagnostic["max_combined_capacity_excess"]<0: problems.append("capacity diagnostics")
            if difference(cap,diagnostic["max_realized_daily_participation"])>RATIO_TOL: problems.append("capacity max reconciliation")
            if difference(metrics.get("capacity_excess"),diagnostic["max_combined_capacity_excess"])>MONEY_TOL: problems.append("capacity summary")
            if diagnostic.get("historical_st_known_observations")!=0 or diagnostic.get("opening_liquidity_verified") is not False or diagnostic.get("posthoc_not_opening_information") is not True: problems.append("invented market truth")
            if terminal.get("model_position_zero")!=computed["book_position_zero"] or terminal.get("fully_exited_by_trades")!=computed["exit_by_trades"]: problems.append("terminal exit/writeoff semantics")
            if difference(terminal.get("residual_value"),computed["terminal_position_value"])>MONEY_TOL or difference(terminal.get("writeoff_loss"),computed["writeoff_loss"])>MONEY_TOL or terminal.get("residual_positions")!=computed["terminal_positions"]: problems.append("terminal values")
            if sum(row["blocked"].values())!=computed["blocked_attempts"]: problems.append("blocked summary")
            wind=int(cfg.get("wind_down_days",0));wind_count=min(wind,len(daily));wind_mask=np.zeros(len(daily),bool)
            if wind_count:wind_mask[-wind_count:]=True
            if not np.array_equal(daily.wind_down.map(truth),wind_mask): problems.append("wind-down calendar")
            terminal_mask=np.zeros(len(daily),bool)
            if cfg.get("liquidate_end") or wind:terminal_mask[-1]=True
            if not np.array_equal(daily.terminal_liquidation.map(truth),terminal_mask):problems.append("terminal calendar")
            if wind_count and (terminal.get("wind_down_start")!=daily.date.iloc[-wind_count] or terminal.get("wind_down_days")!=wind_count):problems.append("wind-down protocol")
            c=candidate_map[row["experiment_id"]]; role=row["period_role"];scenario=row["scenario"]
            if [cfg["start"],cfg["end"]]!=list(protocol["periods"][role]) or cfg["top_n"]!=c["top_n"] or cfg["rebalance"]!=c["rebalance"]:problems.append("candidate/period config")
            if cfg.get("wind_down_days")!=protocol["wind_down_days_frozen_before_this_run"]:problems.append("changed exit window")
            if row["cost_multiplier"]!=(2. if scenario=="stress" else 1.):problems.append("scenario cost multiplier")
            for name in ("max_participation","max_daily_participation"):
                expected_rate=.001 if scenario=="capacity" else protocol["base_config"][name]
                if cfg[name]!=expected_rate:problems.append("scenario "+name)
            expected_cfg=dict(protocol["base_config"]);pool=protocol["universes"][c["universe"]]
            expected_cfg.update(start=protocol["periods"][role][0],end=protocol["periods"][role][1],top_n=c["top_n"],rebalance=c["rebalance"],min_history=max(120,c["required_history"]),universe=pool["universe"],liquidity_top_n=pool["liquidity_top_n"])
            if scenario=="capacity":expected_cfg.update(max_participation=.001,max_daily_participation=.001)
            if cfg!=expected_cfg:problems.append("full frozen configuration")
            started,completed=pd.Timestamp(row["started_at"]).timestamp(),pd.Timestamp(row["completed_at"]).timestamp()
            if started>completed:problems.append("record timestamp order")
            all_starts.append(started)
            if role=="selection_2024":training_completions.append(completed)
            if role=="evaluation_2025":
                evaluation_times.append(min((folder/"record.json").stat().st_mtime,(folder/"daily.csv").stat().st_mtime));evaluation_starts.append(started)
            if not problems:passed_keys.append(run_key)
        except Exception as exc:problems.append("validation exception: "+type(exc).__name__+": "+str(exc))
        primary_errors[run_key]=problems
        check("primary:"+run_key,not problems,errors=problems)
    aggregate={c["experiment_id"]:c for c in study["candidates"]}
    field_map={("selection_2024","base"):"selection_2024",("evaluation_2025","base"):"evaluation_2025",("evaluation_2025","stress"):"stress_2025",("evaluation_2025","capacity"):"capacity_2025"}
    consistent=True
    for row in records:
        candidate=aggregate.get(row["experiment_id"],{});field=field_map.get((row["period_role"],row["scenario"]))
        summary=candidate.get(field,{})
        if summary.get("status")!=row.get("status") or summary.get("folder")!=row.get("folder"):consistent=False
        if any(summary.get(k)!=v for k,v in row.get("metrics",{}).items()):consistent=False
    check("candidate_aggregates_match_primary_records",consistent)
    # Reconstruct selection exclusively from independently recomputed 2024 metrics.
    try:
        selection=study["selection"];frozen=load(out/"selection_frozen_before_2025.json")
        check("selection_matches_frozen_file",selection==frozen)
        train={r["experiment_id"]:(r,recomputed[key(r)]) for r in records if r.get("period_role")=="selection_2024" and r.get("status")=="success" and key(r) in recomputed and not primary_errors.get(key(r))}
        safe=[c for c in grid if c["experiment_id"] in train and train[c["experiment_id"]][1]["mean_invested"]>=.25 and train[c["experiment_id"]][1]["max_drawdown"]>=-.40 and train[c["experiment_id"]][1]["total_return"]>0 and train[c["experiment_id"]][1]["exit_by_trades"]]
        safe.sort(key=lambda c:(-train[c["experiment_id"]][1]["sharpe"],-train[c["experiment_id"]][1]["total_return"],c["experiment_id"]))
        shortlist=safe[:6];ideas={c["idea_id"] for c in shortlist}
        for c in safe:
            if len(shortlist)>=12:break
            if c["idea_id"] not in ideas:shortlist.append(c);ideas.add(c["idea_id"])
        audits=[c["experiment_id"] for c in safe[:3]]
        for fid in ("reversal_w005","lowrange_w020","lowvol_w020"):
            candidates=[c for c in grid if c["factor_id"]==fid and c["experiment_id"] in train]
            candidates.sort(key=lambda c:(-train[c["experiment_id"]][1]["sharpe"],c["experiment_id"]))
            if candidates and candidates[0]["experiment_id"] not in audits:audits.append(candidates[0]["experiment_id"])
        check("training_only_selection_recomputed",selection.get("selected_on")=="selection_2024" and selection.get("selected_id")== (safe[0]["experiment_id"] if safe else None) and selection.get("shortlist")==[c["experiment_id"] for c in shortlist] and selection.get("audit_ids")==audits and selection.get("admissible_count")==len(safe) and selection.get("no_fallback") is True)
        groups=defaultdict(list)
        for c in grid:
            if c["experiment_id"] in train:groups[train[c["experiment_id"]][0]["signal_diagnostics"]["training_signal_signature"]].append(c["experiment_id"])
        observed_groups=sorted(sorted(v) for v in groups.values() if len(v)>1)
        check("training_signature_group_consistency",selection["training_unique_signal_signatures"]==len(groups) and sorted(sorted(v) for v in selection["training_equivalent_groups"])==observed_groups)
        frozen_time=pd.Timestamp(frozen["frozen_at"]).timestamp()
        check("selection_frozen_before_evaluation_files",bool(evaluation_times) and frozen_time<=min(evaluation_times)+.001 and (out/"selection_frozen_before_2025.json").stat().st_mtime<=min(evaluation_times)+.001)
        check("training_completed_before_selection_and_evaluation",bool(training_completions) and bool(evaluation_starts) and max(training_completions)<=frozen_time+.001 and frozen_time<=min(evaluation_starts)+.001)
        check("protocol_frozen_before_all_runs",bool(all_starts) and pd.Timestamp(protocol["frozen_at"]).timestamp()<=min(all_starts)+.001)
    except Exception as exc:check("training_selection_validation_exception",False,error=str(exc))
    details=study.get("detailed_records",[])
    expected_details={"|".join((eid,"evaluation_2025",s)) for eid in study["selection"].get("audit_ids",[]) for s in ("base","stress","capacity")}
    if study["selection"].get("selected_id") in study["selection"].get("audit_ids",[]):expected_details.add("|".join((study["selection"]["selected_id"],"assignment_2025_2026sep","base")))
    detail_index={key(r):r for r in details}
    check("detailed_sample_complete",set(detail_index)==expected_details and len(detail_index)==len(details),expected=len(expected_details),actual=len(details))
    symbols=set()
    for row in details:
        if row.get("status")!="success":continue
        try:
            folder=(out/row["folder"]).resolve();folder.relative_to(out)
            for name in ("trades","holdings","orders"):
                values=frame(folder/(name+".csv"));symbols.update(values.symbol.dropna().astype(str))
        except Exception as exc:check("detail_symbols:"+key(row),False,error=str(exc))
    try:source=Source(symbols,source=SOURCE) if any(r.get("status")=="success" for r in details) else None
    except Exception as exc:source=None;check("detailed_source_load",False,error=str(exc))
    for row in details:
        try:
            if row.get("status")!="success":raise ValueError(row.get("error","detailed simulation failed"))
            if source is None:raise ValueError("Detailed source unavailable")
            report=_detail(out,row,source);detail_reports.append(report)
            check("detailed:"+key(row),report["passed"],max_money_error=report["max_money_error"],violations=report["violations"])
            if key(row) in indexed:
                main=frame(out/indexed[key(row)]["folder"]/"daily.csv");detail=frame(out/row["folder"]/"daily.csv")
                check("detailed_matches_primary:"+key(row),main.equals(detail))
        except Exception as exc:
            report={"key":key(row),"passed":False,"exception":str(exc)};detail_reports.append(report);check("detailed:"+key(row),False,error=str(exc))
    passed=all(c["passed"] for c in checks)
    report={"passed":passed,"status":"passed" if passed else "failed","passed_run_keys":sorted(passed_keys),"checks":checks,
            "money_absolute_tolerance":MONEY_TOL,"detailed_ledgers":detail_reports,
            "created_at":datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "summary":{"candidate_count":len(grid),"expected_primary_runs":len(expected),"observed_primary_runs":len(records),
                       "validated_primary_runs":len(passed_keys),"failed_runs":sum(r.get("status")!="success" for r in records),
                       "checks":len(checks),"failed_checks":sum(not c["passed"] for c in checks),
                       "detailed_sample_size":len(details),"detailed_passed":sum(r["passed"] for r in detail_reports),
                       "max_detailed_money_error":max((r.get("max_money_error",0.) for r in detail_reports),default=0.)},
            "scope":["All primary daily calendars, NAV-derived returns and reported metrics checked independently.",
                     "Detailed source-price cash/positions/NAV/fees/actual-fill turnover/order/capacity reconstruction only for declared detailed sample.",
                     "Training signatures checked for frozen-record consistency, not independently regenerated factor scores.",
                     "Selection timing checked using frozen timestamp and file mtimes; not a tamper-proof provenance proof.",
                     "No real historical ST, auction liquidity, physical corporate actions, or future profitability is verified."]}
    (out/"validation.json").write_text(json.dumps(clean(report),ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    return clean(report)


def validate_study(out, study, records):
    """Always persist an honest failed report for a fatal validation exception."""
    out=Path(out).resolve()
    try:
        return _validate_study(out,study,records)
    except Exception as exc:
        report={"passed":False,"status":"failed","passed_run_keys":[],"checks":[{"name":"fatal_validation_exception","passed":False,"error":type(exc).__name__+": "+str(exc)}],
                "summary":{"candidate_count":len(study.get("candidates",[])),"observed_primary_runs":len(records),"validated_primary_runs":0,"failed_checks":1},
                "detailed_ledgers":[],"money_absolute_tolerance":MONEY_TOL,
                "created_at":datetime.now(timezone(timedelta(hours=8))).isoformat(),"traceback":traceback.format_exc()}
        out.mkdir(parents=True,exist_ok=True)
        (out/"validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
        return report


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("out",type=Path);args=parser.parse_args()
    directory=args.out.resolve()
    pending=directory/("result_pending_validation.json" if (directory/"result_pending_validation.json").exists() else "result.json")
    result=validate_study(directory,load(pending),load(directory/"run_records.json"))
    print(json.dumps({"passed":result["passed"],"summary":result["summary"]},ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
