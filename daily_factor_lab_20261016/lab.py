"""Local daily equity research. No broker connection or real order interface."""
import os
for _key in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"]:
    os.environ.setdefault(_key, "4")

import json
import math
import warnings
import zipfile
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather
from scipy.stats import norm

pa.set_cpu_count(4)
pa.set_io_thread_count(2)
ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("FACTOR_DATA_ZIP", str(ROOT.parent / "feathers.zip")))
ENGINE_VERSION = "4.0.0"
FACTOR_INFO = {
    "momentum60": {"name": "60日动量（跳过5日）", "formula": "C_adj[t-5]/C_adj[t-65]-1", "hypothesis": "中期趋势延续，越高越优"},
    "reversal5": {"name": "5日反转", "formula": "-(C_adj[t]/C_adj[t-5]-1)", "hypothesis": "短期下跌后反弹，越高越优"},
    "lowvol20": {"name": "20日低波动", "formula": "-std(adjusted_daily_returns[t-19:t], ddof=1)", "hypothesis": "较低历史波动对应较稳健表现，越高越优"},
    "liquidity20": {"name": "20日成交金额", "formula": "log(1+mean(amt[t-19:t]))", "hypothesis": "偏好较高流动性，越高越优"},
    "combo": {"name": "四因子等权", "formula": "mean(cross_section_percentile_rank(each of the four factors))", "hypothesis": "固定四个方向及等权，不按结果调权"},
}

@dataclass(frozen=True)
class Config:
    start: str = "2021-01-01"
    end: str = "2026-10-16"
    capital: float = 1_000_000
    top_n: int = 100
    rebalance: str = "weekly"
    min_history: int = 120
    min_price: float = 2.0
    min_avg_amount: float = 10_000_000
    min_active_days20: int = 15
    commission: float = .0003
    min_commission: float = 5
    slippage: float = .0005
    cash_buffer: float = .01
    max_participation: float = .01
    open_guard: float = .048
    writeoff_missing_days: int = 20
    execution_model: str = "legacy"
    st_policy: str = "legacy"
    universe: str = "all_a"
    liquidity_top_n: int = 0
    risk_proxy_window: int = 20
    max_daily_participation: float = .01
    liquidate_end: bool = False
    wind_down_days: int = 0

    def validate(self):
        for key in ["start", "end"]:
            value=getattr(self,key)
            if not isinstance(value,str) or pd.Timestamp(value).strftime("%Y-%m-%d")!=value:
                raise ValueError("日期须为YYYY-MM-DD格式")
        if self.rebalance not in {"daily", "weekly", "monthly"}:
            raise ValueError("调仓频率仅支持 daily/weekly/monthly")
        if not 1 <= self.top_n <= 500 or not 10_000 <= self.capital <= 1e10:
            raise ValueError("持股数须为1–500，资金须为1万元–100亿元")
        if pd.Timestamp(self.start) > pd.Timestamp(self.end):
            raise ValueError("开始日期不能晚于结束日期")
        if not 65 <= self.min_history <= 500 or not 1 <= self.min_active_days20 <= 20:
            raise ValueError("历史门槛须为65–500，20日活跃天数须为1–20")
        for name in ["commission", "slippage"]:
            if not 0 <= getattr(self, name) <= .01: raise ValueError(name + " 超出范围")
        if not 0 <= self.cash_buffer < .5 or not 0 < self.max_participation <= .1:
            raise ValueError("现金缓冲或成交参与率超出范围")
        if not 0 < self.open_guard <= .3 or not 1 <= self.writeoff_missing_days <= 120:
            raise ValueError("开盘门禁或缺失减记阈值超出范围")
        if not 0 <= self.min_commission <= 100 or not 0 <= self.min_price <= 100:
            raise ValueError("最低佣金或最低股价超出范围")
        if not 0 <= self.min_avg_amount <= 1e11:
            raise ValueError("成交金额门槛超出范围")
        if self.execution_model not in {"legacy", "daily_conservative"} or self.st_policy not in {"legacy", "price_proxy", "strict"}:
            raise ValueError("未知执行模型或ST处理政策")
        if self.universe not in {"all_a", "main_a", "growth_a"}:
            raise ValueError("股票范围须为all_a/main_a/growth_a")
        if not 0 <= self.liquidity_top_n <= 6000 or int(self.liquidity_top_n) != self.liquidity_top_n:
            raise ValueError("流动性范围须为0至6000的整数")
        if not 1 <= self.risk_proxy_window <= 120 or int(self.risk_proxy_window) != self.risk_proxy_window:
            raise ValueError("风险代理窗口须为1至120")
        if not 0 < self.max_daily_participation <= .1 or type(self.liquidate_end) is not bool:
            raise ValueError("当日容量参与率或末日清算参数非法")
        if type(self.wind_down_days) is not int or not 0 <= self.wind_down_days <= 120:
            raise ValueError("预定退出窗口wind_down_days须为0至120的整数")
        return self

    @classmethod
    def from_dict(cls, values):
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown: raise ValueError("未知参数: " + ",".join(sorted(unknown)))
        for k, v in values.items():
            if k == "liquidate_end":
                if type(v) is not bool: raise ValueError("liquidate_end须为布尔值")
                continue
            if k not in {"start", "end", "rebalance", "execution_model", "st_policy", "universe"} and (isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)):
                raise ValueError("非法数值参数: " + k)
        for k in ["top_n", "min_history", "min_active_days20", "writeoff_missing_days", "liquidity_top_n", "risk_proxy_window", "wind_down_days"]:
            if k in values and int(values[k]) != values[k]: raise ValueError(k + " 必须为整数")
        values = dict(values)
        for k in ["top_n", "min_history", "min_active_days20", "writeoff_missing_days", "liquidity_top_n", "risk_proxy_window", "wind_down_days"]:
            if k in values: values[k] = int(values[k])
        return cls(**values).validate()

class Market:
    def __init__(self, source=SOURCE):
        self.source = str(source)
        self.raw = {}
        with zipfile.ZipFile(source) as z:
            for f in ["open", "high", "low", "close", "amt", "cumadj", "num_trades"]:
                t = feather.read_table(pa.BufferReader(z.read(f"feathers/{f}.feather")))
                dates = t["date"].to_numpy()
                symbols = t.column_names[1:]
                if not self.raw:
                    self.dates = pd.DatetimeIndex(dates)
                    self.symbols = np.array(symbols)
                assert np.array_equal(self.dates.values, dates) and np.array_equal(self.symbols, symbols)
                self.raw[f] = np.column_stack([t.column(i).to_numpy() for i in range(1, t.num_columns)])
        self.prepare()

    def prepare(self):
        """Causal normalization: per-symbol first valid factor, never final-date factor."""
        self._execution_metadata_cache = {}
        self._eligible_cache = {}
        factors = self.raw["cumadj"]
        ok = np.isfinite(factors) & (factors > 0)
        first = ok.argmax(axis=0)
        self.base_factor = factors[first, np.arange(len(self.symbols))]
        self.scale = np.where(ok, factors / self.base_factor[None, :], np.nan)
        self.open = self.raw["open"] * self.scale
        self.close = self.raw["close"] * self.scale
        self.returns = np.full_like(self.close, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.returns[1:] = self.close[1:] / self.close[:-1] - 1
        self.amount20 = pd.DataFrame(self.raw["amt"]).rolling(20, min_periods=20).mean().to_numpy()
        self.active20 = pd.DataFrame((self.raw["amt"] > 0).astype(float)).rolling(20, min_periods=20).sum().to_numpy()
        self.history = np.cumsum(np.isfinite(self.raw["close"]), axis=0)
        c = pd.DataFrame(self.close)
        self.factors = {
            "momentum60": (c.shift(5) / c.shift(65) - 1).to_numpy(),
            "reversal5": (-(c / c.shift(5) - 1)).to_numpy(),
            "lowvol20": -pd.DataFrame(self.returns).rolling(20, min_periods=20).std(ddof=1).to_numpy(),
            "liquidity20": np.log1p(self.amount20),
        }

    def eligible(self, cfg):
        key = (cfg.min_history, cfg.min_price, cfg.min_avg_amount, cfg.min_active_days20,
               cfg.st_policy, cfg.universe, cfg.liquidity_top_n, cfg.risk_proxy_window)
        if not hasattr(self, "_eligible_cache"): self._eligible_cache = {}
        if key in self._eligible_cache: return self._eligible_cache[key]
        eligible = (self.history >= cfg.min_history) & (self.raw["close"] >= cfg.min_price) & np.isfinite(self.close) & (self.amount20 >= cfg.min_avg_amount) & (self.active20 >= cfg.min_active_days20)
        if cfg.st_policy == "strict":
            raise ValueError("仅zip数据没有真实历史ST字段：strict政策拒绝运行，不能声称剔除真实ST")
        if cfg.st_policy == "price_proxy" or cfg.universe != "all_a" or cfg.liquidity_top_n:
            from execution_metadata import universe_mask, price_risk_proxy
            eligible &= universe_mask(self.symbols, cfg.universe)[None, :]
            if cfg.st_policy == "price_proxy": eligible &= ~price_risk_proxy(self, cfg.risk_proxy_window)
            if cfg.liquidity_top_n:
                ranking = pd.DataFrame(np.where(eligible, self.amount20, np.nan)).rank(axis=1, ascending=False, method="first").to_numpy()
                eligible &= ranking <= cfg.liquidity_top_n
        self._eligible_cache[key] = eligible
        while len(self._eligible_cache) > 12: self._eligible_cache.pop(next(iter(self._eligible_cache)))
        return eligible

    def scores(self, cfg):
        eligible = self.eligible(cfg)
        scores = {f: np.where(eligible & np.isfinite(v), v, np.nan) for f,v in self.factors.items()}
        ranks = [pd.DataFrame(v).rank(axis=1, pct=True, method="average").to_numpy() for v in scores.values()]
        scores["combo"] = np.mean(np.stack(ranks, axis=0), axis=0)
        return eligible, scores

def rebalance_days(dates, indices, freq):
    previous = None
    out = set()
    for i in indices:
        d = dates[i]
        iso = d.isocalendar()
        key = i if freq == "daily" else ((iso.year, iso.week) if freq == "weekly" else (d.year, d.month))
        if key != previous: out.add(int(i))
        previous = key
    return out

def costs(notional, side, date, cfg, multiplier=1):
    if notional <= 1e-10: return {"commission": 0., "slippage": 0., "stamp": 0., "transfer": 0., "total": 0.}
    commission = max(cfg.min_commission, cfg.commission * notional)
    stamp_rate = (.001 if date < pd.Timestamp("2023-08-28") else .0005) if side == "SELL" else 0.
    transfer_rate = .00002 if date < pd.Timestamp("2022-04-29") else .00001
    parts = {"commission": commission * multiplier, "slippage": cfg.slippage * notional * multiplier,
             "stamp": stamp_rate * notional * multiplier, "transfer": transfer_rate * notional * multiplier}
    return {**parts, "total": sum(parts.values())}

def affordable(budget, desired, date, cfg, multiplier):
    if budget <= 0: return 0.
    rate = multiplier * (cfg.commission + cfg.slippage + (.00002 if date < pd.Timestamp("2022-04-29") else .00001))
    available = min(budget / (1 + rate), (budget - cfg.min_commission * multiplier) / (1 + multiplier * (cfg.slippage + (.00002 if date < pd.Timestamp("2022-04-29") else .00001))))
    return max(0., min(desired, available))

def performance(daily, capital):
    if daily.empty: return {}
    nav = daily.nav.to_numpy()
    previous = np.r_[capital, nav[:-1]]
    r = np.divide(nav, previous, out=np.ones_like(nav), where=previous > 0) - 1
    std = float(np.std(r, ddof=1)) if len(r) > 1 else 0.
    total = float(nav[-1] / capital - 1)
    annual = float((nav[-1] / capital) ** (252 / len(r)) - 1) if nav[-1] > 0 else -1.
    peaks = np.maximum.accumulate(np.r_[capital, nav])[1:]
    return {"total_return": total, "annual_return": annual, "annual_vol": std * np.sqrt(252),
            "sharpe": float(np.mean(r) / std * np.sqrt(252)) if std > 0 else 0.,
            "max_drawdown": float(np.min(nav / peaks - 1)), "days": len(r),
            "final_nav": float(nav[-1]), "costs": float(daily.cost.sum()),
            "annual_two_sided_turnover": float(daily.turnover.sum() * 252 / len(r)),
            "mean_invested": float(daily.invested_fraction.mean()), "trades": int(daily.trades.sum()),
            "writeoff_loss": float(daily.writeoff_loss.sum()), "max_stale_fraction": float(daily.stale_fraction.max())}

def simulate(m, score, cfg, cost_multiplier=1., keep_trades=True, progress=None):
    # Public callers may construct Config directly instead of from_dict.
    # Revalidate once at the boundary, never credit negative fee multipliers.
    cfg = Config.from_dict(asdict(cfg))
    if isinstance(cost_multiplier, (bool, np.bool_)) or not isinstance(cost_multiplier, (int, float, np.integer, np.floating)) or not math.isfinite(cost_multiplier) or cost_multiplier < 0:
        raise ValueError("cost_multiplier须为有限非负数，0仅表示gross情景，不接受布尔值")
    cost_multiplier = float(cost_multiplier)
    if cfg.execution_model == "daily_conservative":
        return _simulate_daily_conservative(m, score, cfg, cost_multiplier, keep_trades, progress)
    if cfg.st_policy == "strict":
        raise ValueError("真实历史ST状态缺失，strict政策不可运行")
    if cfg.wind_down_days:
        raise ValueError("预定多日退出窗口须使用daily_conservative执行模型；legacy不支持")
    if cfg.liquidate_end:
        raise ValueError("末日真实约束清算须使用daily_conservative执行模型")
    indices = np.flatnonzero((m.dates >= cfg.start) & (m.dates <= cfg.end) & (np.arange(len(m.dates)) > 0))
    if len(indices) < 20: raise ValueError("区间须至少包含20个交易日")
    schedule = rebalance_days(m.dates, indices, cfg.rebalance)
    n = len(m.symbols)
    units = np.zeros(n)
    targets = np.zeros(n)
    marks = np.zeros(n)
    stale_age = np.zeros(n, dtype=int)
    last_buy = np.full(n, -1, dtype=int)
    cash = float(cfg.capital)
    prev_nav = cash
    rows, ledger = [], []
    blocked = Counter()
    signal_idx = -1
    holdings_final = []
    for i in indices:
        date = m.dates[i]
        open_valid = np.isfinite(m.open[i]) & (m.open[i] > 0)
        close_valid = np.isfinite(m.close[i]) & (m.close[i] > 0)
        open_marks = np.where(open_valid, m.open[i], marks)
        open_nav = cash + float(units @ open_marks)
        # Signal and liquidity limits are frozen at previous close, before the execution date.
        if i in schedule:
            candidates = np.flatnonzero(np.isfinite(score[i-1]))
            ordered = candidates[np.lexsort((m.symbols[candidates], -score[i-1, candidates]))]
            selected = ordered[:cfg.top_n]
            targets[:] = 0
            # For an unavailable opening quote, retain a pending target sized at the
            # previous known close, without replacing the failed name with a lower rank.
            sizing_prices = np.where(open_valid, m.open[i], m.close[i-1])
            valid_selected = selected[np.isfinite(sizing_prices[selected]) & (sizing_prices[selected] > 0)]
            if len(selected):
                budget_per_name = open_nav * (1 - cfg.cash_buffer) / len(selected)
                targets[valid_selected] = budget_per_name / sizing_prices[valid_selected]
            signal_idx = i-1
        # Failures are skipped; unfilled target orders may retry until the next scheduled rebalance.
        price_reference = np.full(n, np.nan)
        valid_reference = np.isfinite(m.raw["close"][i-1]) & (m.raw["cumadj"][i-1] > 0) & (m.raw["cumadj"][i] > 0)
        price_reference[valid_reference] = m.raw["close"][i-1, valid_reference] * m.raw["cumadj"][i-1, valid_reference] / m.raw["cumadj"][i, valid_reference]
        with np.errstate(invalid="ignore", divide="ignore"):
            open_move = m.raw["open"][i] / price_reference - 1
        day_cost, day_turnover, trade_count, writeoff = 0., 0., 0, 0.
        day_blocked = 0
        capacity = cfg.max_participation * np.nan_to_num(m.amount20[i-1], nan=0.)
        # Current daily amount only rejects executions on sessions with no trades; it never ranks stocks.
        can_trade = open_valid & (m.raw["amt"][i] > 0) & np.isfinite(open_move)
        for side in ["SELL", "BUY"]:
            delta = targets - units
            candidates = np.flatnonzero((delta < -1e-9) if side == "SELL" else (delta > 1e-9))
            if side == "BUY" and len(candidates):
                candidates = candidates[np.lexsort((m.symbols[candidates], -score[max(signal_idx, 0), candidates]))]
            for j in candidates:
                reason = None
                if not can_trade[j]: reason = "missing_open_factor_or_no_daily_turnover"
                elif (side == "BUY" and open_move[j] >= cfg.open_guard) or (side == "SELL" and open_move[j] <= -cfg.open_guard): reason = "conservative_open_move_guard"
                elif side == "SELL" and last_buy[j] == i: reason = "T_plus_1"
                if reason:
                    blocked[reason] += 1
                    day_blocked += 1
                    continue
                desired = abs(float(delta[j])) * m.open[i,j]
                notional = min(desired, float(capacity[j]))
                if side == "BUY": notional = affordable(cash, notional, date, cfg, cost_multiplier)
                if notional < 1.: continue
                # Orders obey basic raw-share lot constraints. Effective existing
                # shares remain synthetic because factors cannot separate cash/splits.
                board_minimum = 200 if str(m.symbols[j]).startswith(("688", "689")) else 100
                raw_shares = notional / m.raw["open"][i,j]
                liquidation = side == "SELL" and targets[j] <= 1e-9 and desired <= capacity[j] + 1e-7
                if not liquidation:
                    raw_shares = math.floor((raw_shares + 1e-7) / 100) * 100
                    if raw_shares < board_minimum:
                        blocked["below_lot_or_budget"] += 1
                        day_blocked += 1
                        continue
                qty = raw_shares / m.scale[i,j]
                notional = qty * m.open[i,j]
                fee = costs(notional, side, date, cfg, cost_multiplier)
                if side == "SELL" and fee["total"] >= notional:
                    blocked["sale_value_below_fees"] += 1
                    continue
                if side == "SELL":
                    units[j] -= qty
                    cash += notional - fee["total"]
                else:
                    units[j] += qty
                    cash -= notional + fee["total"]
                    last_buy[j] = i
                day_cost += fee["total"]
                day_turnover += notional
                trade_count += 1
                if keep_trades:
                    ledger.append({"date": date.strftime("%Y-%m-%d"), "signal_date": m.dates[signal_idx].strftime("%Y-%m-%d"),
                        "symbol": str(m.symbols[j]), "side": side, "adjusted_units": qty,
                        "raw_share_equivalent": qty * m.scale[i,j], "open_raw": m.raw["open"][i,j],
                        "fill_price_raw": m.raw["open"][i,j] * (1 + (1 if side == "BUY" else -1) * cfg.slippage * cost_multiplier),
                        "notional": notional, **fee, "cash_after": cash})
        # Missing observations freeze the last valuation, then a fixed causal delay causes a write-off.
        held = units > 1e-9
        stale_age = np.where(held & ~close_valid, stale_age + 1, 0)
        marks = np.where(close_valid, m.close[i], np.where(open_valid, m.open[i], marks))
        expired = held & (stale_age >= cfg.writeoff_missing_days)
        for j in np.flatnonzero(expired):
            loss = float(units[j] * marks[j])
            writeoff += loss
            if keep_trades: ledger.append({"date": date.strftime("%Y-%m-%d"), "signal_date": "", "symbol": str(m.symbols[j]), "side": "WRITE_OFF", "notional": loss, "total": 0., "cash_after": cash})
        units[expired] = 0
        nav = cash + float(units @ marks)
        stale_value = float(np.sum(units[~close_valid] * marks[~close_valid]))
        if cash < -1e-5 or np.min(units) < -1e-6 or not np.isfinite(nav):
            raise AssertionError("Cash/position accounting invariant failed")
        rows.append({"date": date.strftime("%Y-%m-%d"), "nav": nav, "return": nav / prev_nav - 1 if prev_nav > 0 else 0.,
            "cash": cash, "positions": int(np.count_nonzero(units > 1e-9)),
            "invested_fraction": 1 - cash/nav if nav else 0., "cost": day_cost,
            "turnover": day_turnover / open_nav if open_nav > 0 else 0., "trades": trade_count,
            "blocked_attempts": day_blocked, "writeoff_loss": writeoff,
            "stale_fraction": stale_value/nav if nav else 0., "rebalance": bool(i in schedule)})
        prev_nav = nav
    daily = pd.DataFrame(rows)
    for j in np.flatnonzero(units > 1e-9):
        holdings_final.append({"symbol": str(m.symbols[j]), "adjusted_units": float(units[j]), "value": float(units[j]*marks[j]), "weight": float(units[j]*marks[j]/prev_nav), "stale_days": int(stale_age[j])})
    return {"daily": daily, "trades": pd.DataFrame(ledger), "holdings": pd.DataFrame(holdings_final),
            "metrics": performance(daily, cfg.capital), "blocked": dict(blocked),
            "orders": pd.DataFrame(), "terminal": {"requested": False, "fully_liquidated": None},
            "execution_diagnostics": {"execution_model": "legacy", "unchanged_v1_execution": True,
                                      "historical_st_status": "unavailable", "opening_liquidity_verified": False}}

def _simulate_daily_conservative(m, score, cfg, cost_multiplier=1., keep_trades=True, progress=None):
    """Frozen-close orders plus explicitly post-session feasibility audits.

    This daily-bar scenario cannot verify auction liquidity. Adjusted units
    remain synthetic total-return positions, not physical settlement shares.
    """
    from execution_metadata import (board_labels, corporate_action_mask, metadata_summary,
                                    round_price_half_up, theoretical_limits)
    if cfg.st_policy == "strict":
        raise ValueError("仅zip无真实历史ST状态：strict政策拒绝运行")
    indices = np.flatnonzero((m.dates >= cfg.start) & (m.dates <= cfg.end) & (np.arange(len(m.dates)) > 0))
    if len(indices) < 20: raise ValueError("区间须至少包含20个交易日")
    score = np.asarray(score, dtype=float)
    if score.shape != m.close.shape: raise ValueError("评分矩阵维度须与市场数据一致")
    eligible = m.eligible(cfg)
    schedule = rebalance_days(m.dates, indices, cfg.rebalance)
    boards, actions = board_labels(m.symbols), corporate_action_mask(m)
    previous_factor = m.raw["cumadj"]
    n = len(m.symbols)
    units, targets, marks = np.zeros(n), np.zeros(n), np.zeros(n)
    stale_age, last_buy = np.zeros(n, dtype=int), np.full(n, -1, dtype=int)
    cash = prev_nav = float(cfg.capital)
    rows, ledger, orders, holdings = [], [], [], []
    blocked, filled_reasons, final_blocked, wind_down_rejections = Counter(), Counter(), Counter(), Counter()
    signal_idx = -1
    wind_down_count = min(cfg.wind_down_days, len(indices))
    wind_down_start = int(indices[-wind_down_count]) if wind_down_count else None
    clipped_total = max_participation = max_capacity_excess = 0.
    for i in indices:
        date, opening, raw_open = m.dates[i], m.open[i], m.raw["open"][i]
        date_text = date.strftime("%Y-%m-%d")
        open_valid = np.isfinite(opening) & (opening > 0) & np.isfinite(raw_open) & (raw_open > 0)
        close_valid = np.isfinite(m.close[i]) & (m.close[i] > 0)
        open_nav = cash + float(units @ np.where(open_valid, opening, marks))
        # Exit dates are preplanned from the declared source calendar. Neither
        # future prices nor future volume determine this window's starting day.
        winding_down = bool(wind_down_count and i >= wind_down_start)
        ending = bool((cfg.liquidate_end or wind_down_count) and i == indices[-1])
        exit_day = bool(winding_down or ending)
        if exit_day:
            targets[:] = 0.
            signal_idx = i - 1
        elif i in schedule:
            candidates = np.flatnonzero(eligible[i-1] & np.isfinite(score[i-1]) & np.isfinite(m.close[i-1]) & (m.close[i-1] > 0))
            selected = candidates[np.lexsort((m.symbols[candidates], -score[i-1, candidates]))][:cfg.top_n]
            targets[:] = 0.
            if len(selected):
                budget = prev_nav * (1 - cfg.cash_buffer) / len(selected)
                targets[selected] = budget / m.close[i-1, selected]
            signal_idx = i - 1
        prior_amount = np.nan_to_num(m.amount20[i-1], nan=0., posinf=0., neginf=0.)
        today_amount = np.nan_to_num(m.raw["amt"][i], nan=0., posinf=0., neginf=0.)
        capacity_lag = np.maximum(0., prior_amount * cfg.max_participation)
        capacity_day = np.maximum(0., today_amount * cfg.max_daily_participation)
        capacity, used = np.minimum(capacity_lag, capacity_day), np.zeros(n)
        upper, lower = theoretical_limits(m, i, boards)
        reference = m.raw["close"][i-1]
        st_upper, st_lower = round_price_half_up(reference * 1.05), round_price_half_up(reference * .95)
        with np.errstate(invalid="ignore", divide="ignore"):
            move = raw_open / reference - 1.
        day_cost = turnover = writeoff = 0.
        trades = attempts_blocked = 0
        for side in ("SELL", "BUY"):
            delta = targets - units
            candidates = np.flatnonzero(delta < -1e-9) if side == "SELL" else np.flatnonzero(delta > 1e-9)
            if side == "BUY" and len(candidates):
                candidates = candidates[np.lexsort((m.symbols[candidates], -score[max(signal_idx, 0), candidates]))]
            for j in candidates:
                requested_units = abs(float(delta[j]))
                desired = requested_units * opening[j] if open_valid[j] else 0.
                requested_raw = requested_units * m.scale[i,j] if np.isfinite(m.scale[i,j]) else None
                available = max(0., float(capacity[j] - used[j]))
                reason, fill = None, None
                if exit_day and side == "BUY": reason = "terminal_no_buys"
                elif side == "BUY" and not eligible[i-1,j]: reason = "no_longer_signal_eligible"
                elif side == "BUY" and not np.isfinite(score[i-1,j]): reason = "no_longer_finite_score_or_filter"
                elif boards[j] == "unknown": reason = "unknown_board"
                elif not open_valid[j]: reason = "missing_open_or_factor"
                elif today_amount[j] <= 0: reason = "no_daily_turnover_posthoc_audit"
                elif actions[i,j]: reason = "factor_jump_posthoc_action_audit"
                elif not np.isfinite(previous_factor[i-1,j]) or previous_factor[i-1,j] <= 0: reason = "unverified_previous_factor_reference"
                elif not np.isfinite(reference[j]) or reference[j] <= 0: reason = "missing_previous_reference"
                elif not np.isfinite(m.raw["high"][i,j]) or not np.isfinite(m.raw["low"][i,j]): reason = "missing_ohlc_posthoc_audit"
                elif side == "SELL" and last_buy[j] == i: reason = "T_plus_1"
                elif side == "BUY" and raw_open[j] >= upper[j] - .0049: reason = "theoretical_upper_limit"
                elif side == "SELL" and raw_open[j] <= lower[j] + .0049: reason = "theoretical_lower_limit"
                elif boards[j] == "main" and side == "BUY" and raw_open[j] >= st_upper[j] - .0049: reason = "unknown_ST_rounded_5pct_upper_guard"
                elif boards[j] == "main" and side == "SELL" and raw_open[j] <= st_lower[j] + .0049: reason = "unknown_ST_rounded_5pct_lower_guard"
                elif (side == "BUY" and move[j] >= cfg.open_guard) or (side == "SELL" and move[j] <= -cfg.open_guard): reason = "conservative_open_move_guard"
                else:
                    impacted = raw_open[j] * (1 + (1 if side == "BUY" else -1) * cfg.slippage * cost_multiplier)
                    fill = (math.ceil(impacted * 100 - 1e-8) if side == "BUY" else math.floor(impacted * 100 + 1e-8)) / 100.
                    if fill <= 0: reason = "nonpositive_tick_fill"
                    elif fill > m.raw["high"][i,j] + .0049 or fill < m.raw["low"][i,j] - .0049: reason = "slipped_price_outside_daily_range_posthoc_audit"
                    elif side == "BUY" and fill >= upper[j] - .0049: reason = "tick_fill_at_theoretical_upper_limit"
                    elif side == "SELL" and fill <= lower[j] + .0049: reason = "tick_fill_at_theoretical_lower_limit"
                    elif boards[j] == "main" and side == "BUY" and fill >= st_upper[j] - .0049: reason = "tick_fill_at_unknown_ST_upper_guard"
                    elif boards[j] == "main" and side == "SELL" and fill <= st_lower[j] + .0049: reason = "tick_fill_at_unknown_ST_lower_guard"
                desired_execution = requested_units * m.scale[i,j] * fill if reason is None else 0.
                execution_notional = min(desired_execution, available) if reason is None else 0.
                if reason is None and execution_notional < 1.: reason = "daily_capacity_exhausted_or_tiny_order"
                if reason is None:
                    if side == "BUY":
                        transfer = .00002 if date < pd.Timestamp("2022-04-29") else .00001
                        rate = cost_multiplier * (cfg.commission + transfer)
                        minimum_budget = (cash - cfg.min_commission * cost_multiplier) / (1 + cost_multiplier * transfer)
                        execution_notional = max(0., min(execution_notional, cash / (1 + rate), minimum_budget))
                    raw_qty = execution_notional / fill
                    full_sale = side == "SELL" and targets[j] <= 1e-9 and desired_execution <= available + 1e-7
                    if full_sale:
                        raw_qty = min(raw_qty, float(units[j] * m.scale[i,j]))
                    else:
                        step, minimum = (1, 200) if boards[j] == "star" else (100, 100)
                        raw_qty = math.floor((raw_qty + 1e-8) / step) * step
                        if raw_qty < minimum: reason = "below_lot_or_budget"
                    if reason is None and raw_qty <= 0: reason = "zero_quantity"
                    if reason is None:
                        qty = min(raw_qty / m.scale[i,j], float(units[j])) if side == "SELL" else raw_qty / m.scale[i,j]
                        raw_qty, notional = qty * m.scale[i,j], qty * opening[j]
                        execution_notional = raw_qty * fill
                        fee = costs(execution_notional, side, date, cfg, cost_multiplier)
                        fee["slippage"] = abs(fill - raw_open[j]) * raw_qty
                        fee["total"] = sum(fee[key] for key in ["commission", "slippage", "stamp", "transfer"])
                        if side == "BUY" and notional + fee["total"] > cash + 1e-6: reason = "cash_precision_guard"
                        elif side == "SELL" and fee["total"] >= notional: reason = "sale_value_below_fees"
                order = {"date": date_text, "signal_date": m.dates[max(signal_idx, 0)].strftime("%Y-%m-%d"),
                         "symbol": str(m.symbols[j]), "side": side, "requested_notional": float(desired),
                         "requested_execution_notional": float(desired_execution),
                         "requested_raw_share_equivalent": requested_raw, "capacity_lagged": float(capacity_lag[j]),
                         "capacity_daily_posthoc": float(capacity_day[j]), "target_adjusted_units": float(targets[j]),
                         "terminal": ending, "wind_down": winding_down}
                if reason is not None:
                    blocked[reason] += 1
                    attempts_blocked += 1
                    if ending: final_blocked[reason] += 1
                    if winding_down: wind_down_rejections[reason] += 1
                    if keep_trades: orders.append({**order, "status": "REJECTED", "reason": reason, "filled_notional": 0., "filled_execution_notional": 0.})
                    continue
                if side == "SELL":
                    units[j] = max(0., units[j] - qty)
                    cash += notional - fee["total"]
                else:
                    units[j] += qty
                    cash -= notional + fee["total"]
                    last_buy[j] = i
                if abs(cash) < 1e-8: cash = 0.
                used[j] += execution_notional
                clipped_total += max(0., desired_execution - min(desired_execution, available))
                day_cost, turnover, trades = day_cost + fee["total"], turnover + execution_notional, trades + 1
                status = "partial" if qty < requested_units - 1e-7 else "full"
                filled_reasons[status] += 1
                if ending and status == "partial":
                    final_blocked["partial_capacity_remaining" if desired_execution > available + 1e-7 else "partial_lot_remaining"] += 1
                if winding_down and status == "partial":
                    wind_down_rejections["partial_capacity_remaining" if desired_execution > available + 1e-7 else "partial_lot_remaining"] += 1
                if keep_trades:
                    ledger.append({"date": date_text, "signal_date": order["signal_date"], "symbol": str(m.symbols[j]), "side": side,
                                   "adjusted_units": float(qty), "raw_share_equivalent": float(raw_qty), "open_raw": float(raw_open[j]),
                                   "fill_price_raw": float(fill), "notional": float(notional), "execution_notional": float(execution_notional),
                                   **fee, "cash_after": float(cash),
                                   "board": str(boards[j]), "execution_model": cfg.execution_model, "terminal": ending,
                                   "wind_down": winding_down})
                    orders.append({**order, "status": "FILLED", "reason": status, "filled_notional": float(notional),
                                   "filled_execution_notional": float(execution_notional)})
        with np.errstate(invalid="ignore", divide="ignore"):
            participation = np.divide(used, today_amount, out=np.zeros(n), where=today_amount > 0)
        max_participation = max(max_participation, float(participation.max(initial=0)))
        max_capacity_excess = max(max_capacity_excess, float(np.maximum(used - capacity, 0).max(initial=0)))
        held = units > 1e-9
        stale_age = np.where(held & ~close_valid, stale_age + 1, 0)
        marks = np.where(close_valid, m.close[i], np.where(open_valid, opening, marks))
        expired = held & (stale_age >= cfg.writeoff_missing_days)
        for j in np.flatnonzero(expired):
            loss = float(units[j] * marks[j])
            writeoff += loss
            if keep_trades: ledger.append({"date": date_text, "signal_date": "", "symbol": str(m.symbols[j]), "side": "WRITE_OFF", "notional": loss, "total": 0., "cash_after": cash})
        units[expired] = 0.
        nav = cash + float(units @ marks)
        stale_value = float(np.sum(units[~close_valid] * marks[~close_valid]))
        if cash < -1e-5 or np.min(units) < -1e-7 or not np.isfinite(nav): raise AssertionError("Conservative accounting invariant failed")
        rows.append({"date": date_text, "nav": nav, "return": nav / prev_nav - 1 if prev_nav > 0 else 0., "cash": cash,
                     "positions": int(np.count_nonzero(units > 1e-9)), "invested_fraction": 1 - cash/nav if nav else 0., "cost": day_cost,
                     "turnover": turnover / open_nav if open_nav > 0 else 0., "trades": trades, "blocked_attempts": attempts_blocked,
                     "writeoff_loss": writeoff, "stale_fraction": stale_value/nav if nav else 0., "rebalance": bool(i in schedule),
                     "terminal_liquidation": ending, "daily_capacity_used": float(used.sum()),
                     "daily_capacity_max_participation": float(participation.max(initial=0)), "wind_down": winding_down})
        prev_nav = nav
    daily = pd.DataFrame(rows)
    for j in np.flatnonzero(units > 1e-9):
        holdings.append({"symbol": str(m.symbols[j]), "adjusted_units": float(units[j]), "value": float(units[j]*marks[j]),
                         "weight": float(units[j]*marks[j]/prev_nav) if prev_nav else 0., "stale_days": int(stale_age[j]),
                         "raw_share_equivalent": float(units[j]*m.scale[indices[-1],j]) if np.isfinite(m.scale[indices[-1],j]) else None})
    diagnostics = metadata_summary(m, cfg, indices)
    diagnostics.update({"execution_model": cfg.execution_model, "engine_version": ENGINE_VERSION,
                        "target_timing": "previous-close NAV and price; no opening-price sizing",
                        "posthoc_audits": ["current daily amount cap", "current OHLC rejection", "current factor-jump rejection"],
                        "posthoc_not_opening_information": True, "opening_liquidity_verified": False,
                        "max_realized_daily_participation": max_participation, "max_combined_capacity_excess": max_capacity_excess,
                        "requested_notional_capacity_clipped": clipped_total, "filled_order_types": dict(filled_reasons),
                        "tick_slippage": "buy ceil/sell floor to 0.01; expense equals tick fill minus raw open",
                        "ledger_notional_definition": "notional=raw-open reference amount; execution_notional=tick fill amount; fees and capacities use execution_notional",
                        "turnover_definition": "sum of execution_notional divided by opening NAV; includes both buy and sell sides",
                        "pending_buy_policy": "frozen targets; require previous-close base eligibility AND finite current score/filter; no reranking until rebalance",
                        "max_actual_positions": int(daily.positions.max()), "top_n_is_target_not_hard_holding_cap": True,
                        "unknown_main_board_ST_guard": "rounded hypothetical 5% guards, not official ST labels"})
    terminal = {"requested": bool(cfg.liquidate_end or wind_down_count), "date": str(m.dates[indices[-1]].date()), "residual_positions": len(holdings),
                "residual_value": float(sum(h["value"] for h in holdings)), "fully_liquidated": bool(not holdings) if (cfg.liquidate_end or wind_down_count) else None,
                "blocked_reasons": dict(final_blocked), "synthetic_fractional_share_equivalents_possible": True,
                "wind_down_start": str(m.dates[wind_down_start].date()) if wind_down_count else None,
                "wind_down_days_requested": cfg.wind_down_days, "wind_down_days": wind_down_count,
                "wind_down_rejections": dict(wind_down_rejections),
                "model_position_zero": bool(not holdings),
                "fully_exited_by_trades": bool(not holdings and daily.writeoff_loss.sum() == 0.) if (cfg.liquidate_end or wind_down_count) else None,
                "writeoff_loss": float(daily.writeoff_loss.sum()),
                "fully_liquidated_semantics": "compatibility field: zero remaining synthetic model positions, possibly after write-off; use fully_exited_by_trades for exit claims",
                "liquidation_attempt_days": wind_down_count if wind_down_count else int(cfg.liquidate_end),
                "wind_down_schedule_basis": "last N source trading dates in the declared interval, preplanned calendar only; no future price or turnover selection"}
    trade_columns = ["date", "signal_date", "symbol", "side", "adjusted_units", "raw_share_equivalent", "open_raw", "fill_price_raw",
                     "notional", "execution_notional", "commission", "slippage", "stamp", "transfer", "total", "cash_after", "board", "execution_model", "terminal", "wind_down"]
    order_columns = ["date", "signal_date", "symbol", "side", "requested_notional", "requested_raw_share_equivalent", "capacity_lagged",
                     "capacity_daily_posthoc", "target_adjusted_units", "terminal", "wind_down", "status", "reason", "filled_notional", "requested_execution_notional", "filled_execution_notional"]
    holding_columns = ["symbol", "adjusted_units", "value", "weight", "stale_days", "raw_share_equivalent"]
    return {"daily": daily, "trades": pd.DataFrame(ledger, columns=trade_columns), "holdings": pd.DataFrame(holdings, columns=holding_columns),
            "metrics": performance(daily, cfg.capital), "blocked": dict(blocked), "orders": pd.DataFrame(orders, columns=order_columns),
            "terminal": terminal, "execution_diagnostics": diagnostics}


def ic_analysis(m, score, eligible, cfg):
    # Signal at close t, entry at open t+1, exit at open t+6: five source-date intervals.
    forward = np.full_like(m.open, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        forward[:-6] = m.open[6:] / m.open[1:-5] - 1
    records = []
    for i in np.flatnonzero((m.dates >= cfg.start) & (m.dates <= cfg.end)):
        if i + 6 >= len(m.dates) or m.dates[i+6] > pd.Timestamp(cfg.end): continue
        mask = eligible[i] & np.isfinite(score[i]) & np.isfinite(forward[i])
        count = int(mask.sum())
        if count < 30 or np.ptp(score[i,mask]) == 0 or np.ptp(forward[i,mask]) == 0: continue
        a = pd.Series(score[i,mask]).rank(method="average").to_numpy()
        b = pd.Series(forward[i,mask]).rank(method="average").to_numpy()
        ic = float(np.corrcoef(a,b)[0,1])
        records.append({"date": m.dates[i].strftime("%Y-%m-%d"), "rank_ic": ic, "valid_pairs": count,
                        "eligible": int(np.count_nonzero(eligible[i] & np.isfinite(score[i]))), "pair_coverage": count / max(1,int(np.count_nonzero(eligible[i] & np.isfinite(score[i]))))})
    daily = pd.DataFrame(records)
    if daily.empty: return daily, {}
    x = daily.rank_ic.to_numpy()
    n = len(x)
    centered = x - x.mean()
    longrun = float(centered @ centered / n)
    for lag in range(1, min(5, n-1)+1):
        longrun += 2*(1-lag/6)*float(centered[lag:] @ centered[:-lag]/n)
    se = math.sqrt(max(longrun, 0)/n)
    mean = float(np.mean(x))
    return daily, {"mean_rank_ic": mean, "ic_std": float(np.std(x,ddof=1)), "days": n,
        "hac_lags": 5, "hac_se": se, "ci95_low": mean-1.96*se, "ci95_high": mean+1.96*se,
        "p_raw_hac_normal": float(2*norm.sf(abs(mean/se))) if se else None,
        "positive_fraction": float(np.mean(x>0)), "mean_pair_coverage": float(daily.pair_coverage.mean()),
        "definition": "Cross-sectional Spearman factor(t) vs adjusted open(t+6)/open(t+1)-1; complete outcome pairs; HAC lag5 normal inference, raw P only"}

def reference_curve(m, eligible, cfg, indices):
    # Non-investable cost-free reference, denominator fixed using yesterday's universe.
    value = cfg.capital
    out = []
    for i in indices:
        mask = eligible[i-1]
        r = np.where(np.isfinite(m.returns[i]), m.returns[i], 0.)
        value *= 1 + (float(np.mean(r[mask])) if mask.any() else 0.)
        out.append({"date": m.dates[i].strftime("%Y-%m-%d"), "nav": value,
                    "eligible_count": int(mask.sum()), "missing_returns_as_zero": int(np.count_nonzero(mask & ~np.isfinite(m.returns[i])))})
    return pd.DataFrame(out)
