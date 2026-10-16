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
        return self

    @classmethod
    def from_dict(cls, values):
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown: raise ValueError("未知参数: " + ",".join(sorted(unknown)))
        for k, v in values.items():
            if k not in {"start", "end", "rebalance"} and (isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)):
                raise ValueError("非法数值参数: " + k)
        for k in ["top_n", "min_history", "min_active_days20", "writeoff_missing_days"]:
            if k in values and int(values[k]) != values[k]: raise ValueError(k + " 必须为整数")
        values = dict(values)
        for k in ["top_n", "min_history", "min_active_days20", "writeoff_missing_days"]:
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
        return (self.history >= cfg.min_history) & (self.raw["close"] >= cfg.min_price) & np.isfinite(self.close) & (self.amount20 >= cfg.min_avg_amount) & (self.active20 >= cfg.min_active_days20)

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
            "metrics": performance(daily, cfg.capital), "blocked": dict(blocked)}

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
