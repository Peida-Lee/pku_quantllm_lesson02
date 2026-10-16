"""Causal, ZIP-only price/amount factors. No performance-based direction selection.

Each compute() returns a signed score: larger is the prespecified preferred
direction. NaN means unavailable, never zero. Parameterizations count as
specifications, not independent economic hypotheses. Signals are close-known
and may first be submitted on the next source trading date.
"""
from collections import OrderedDict
from copy import deepcopy

import numpy as np
import pandas as pd


SOURCE_FIELDS = ("open", "high", "low", "close", "amt", "num_trades", "cumadj")
WINDOWS = (10, 20, 60)
COMMON_BOUNDARIES = [
    "仅原包七矩阵；amt按人民币元解释是未经外部核实的单位假设。",
    "使用原始价格×正累计复权比例的合成总回报价格；不等于可交收股数或现金分红。",
    "截至t收盘计算，最早t+1执行；无历史ST真值、官方限价、竞价深度证明。",
    "缺失、不正价格/复权因子、负成交额/笔数不填零；滚动窗口必须完整有效。",
    "方向为探索前固定假设，不保证收益；参数规格和相关因子不等于独立idea。",
]

# key: Chinese name, idea family, unsigned formula, direction, source inputs,
# primitive lookback overhead (a w-return window needs w+1 source rows).
_FAMILIES = {
    "trend_efficiency": ("趋势路径效率", "momentum", "(C[t]/C[t-w]-1)/sum_w(abs(r))", 1, ("close",), 1),
    "return_to_risk": ("收益波动比", "momentum", "mean_w(r)/std_w(r,ddof=1)", 1, ("close",), 1),
    "lowvol": ("低收益波动", "risk", "std_w(r,ddof=1)", -1, ("close",), 1),
    "downside_risk": ("低下行均方风险", "risk", "sqrt(mean_w(min(r,0)^2))", -1, ("close",), 1),
    "tail_return": ("较高左尾收益", "risk", "quantile_w(r,q=0.1,interpolation=linear)", 1, ("close",), 1),
    "positive_fraction": ("上涨日期占比", "momentum", "mean_w(1[r>0])", 1, ("close",), 1),
    "intraday_trend": ("日内收益延续", "price_structure", "mean_w(C/O-1)", 1, ("open", "close"), 0),
    "overnight_reversal": ("隔夜缺口反转", "price_structure", "mean_w(O[t]/C[t-1]-1)", -1, ("open", "close"), 1),
    "lowrange": ("低日内振幅", "risk", "mean_w((H-L)/C)", -1, ("high", "low", "close"), 0),
    "closing_position": ("日内收盘强度", "price_structure", "mean_w((2*C-H-L)/(H-L))", 1, ("high", "low", "close"), 0),
    "high_position": ("接近历史高点", "price_structure", "C[t]/max_w(H)-1", 1, ("high", "close"), 0),
    "channel_position": ("历史区间位置", "price_structure", "(C[t]-min_w(L))/(max_w(H)-min_w(L))", 1, ("high", "low", "close"), 0),
    "mean_reversion": ("价格均线偏离反转", "reversal", "C[t]/mean_w(C)-1", -1, ("close",), 0),
    "range_surprise": ("低当日振幅冲击", "risk", "((H[t]-L[t])/C[t])/mean_w((H-L)/C)", -1, ("high", "low", "close"), 0),
    "gap_volatility": ("低隔夜缺口波动", "risk", "std_w(O[t]/C[t-1]-1,ddof=1)", -1, ("open", "close"), 1),
    "intraday_volatility": ("低日内收益波动", "risk", "std_w(C/O-1,ddof=1)", -1, ("open", "close"), 0),
    "liquidity": ("高平均成交额", "liquidity", "log1p(mean_w(A))", 1, ("amt",), 0),
    "amount_stability": ("低成交额对数波动", "liquidity", "std_w(log1p(A),ddof=1)", -1, ("amt",), 0),
    "amihud": ("低单位金额价格冲击代理", "liquidity", "mean_w(abs(r)/(A/1e8))", -1, ("close", "amt"), 1),
    "return_amount_corr": ("低收益成交额相关", "price_amount", "corr_w(r,log1p(A))", -1, ("close", "amt"), 1),
    "amount_trend": ("短期成交额扩张", "price_amount", "log(mean_5(A)/mean_w(A))", 1, ("amt",), 0),
    "trade_intensity": ("高平均成交笔数", "liquidity", "log1p(mean_w(N))", 1, ("num_trades",), 0),
    "average_trade_value": ("高平均每笔成交额", "liquidity", "log1p(mean_w(A/N))", 1, ("amt", "num_trades"), 0),
}


def _make_specs():
    specs = {}

    def add(idea, w, name, family, raw_formula, direction, fields, overhead):
        fid = f"{idea}_w{w:03d}"
        fields = list(fields)
        if any(f in fields for f in ("open", "high", "low", "close")):
            fields.append("cumadj")
        specs[fid] = {
            "id": fid, "name": f"{w}日{name}", "idea_id": idea, "family": family,
            "window": w, "min_history": w + overhead,
            "required_history_rows": w + overhead,
            "direction": direction, "raw_formula": raw_formula,
            "formula": f"{'-' if direction < 0 else ''}({raw_formula})",
            "required_fields": fields,
            "parameterization": {"window": w, **({"skip": 5} if idea == "momentum_skip5" else {})},
            "validity": "每个所需原语在截至t的完整尾随窗口有效；所有分母严格正，相关/波动比要求正方差；输出有限。",
            "signal_timing": "known after close t; first execution opportunity is t+1",
            "boundaries": list(COMMON_BOUNDARIES),
            "counting_note": "这是一个预先规定参数规格；同族窗口不算独立经济idea，未将相同窗口正反方向重复计数。",
        }

    for w in (1, 5, 10):
        add("reversal", w, "收盘收益反转", "reversal", "C[t]/C[t-w]-1", -1, ("close",), 1)
    for w in (20, 60, 120):
        add("momentum", w, "收盘动量", "momentum", "C[t]/C[t-w]-1", 1, ("close",), 1)
        add("momentum_skip5", w, "跳过5日动量", "momentum", "C[t-5]/C[t-w-5]-1", 1, ("close",), 6)
    for idea, (name, family, formula, direction, fields, overhead) in _FAMILIES.items():
        for w in WINDOWS:
            add(idea, w, name, family, formula, direction, fields, overhead)
    return specs


FACTOR_SPECS = _make_specs()
BASELINE_EQUIVALENTS = {
    "dr5_reversal": "reversal_w005", "reversal1": "reversal_w001",
    "momentum20": "momentum_w020", "momentum60": "momentum_skip5_w060",
    "lowvol20": "lowvol_w020", "liquidity20": "liquidity_w020",
    "lowrange20": "lowrange_w020",
}


def catalog():
    """Return independent JSON-serializable metadata, ordered by stable ID."""
    return deepcopy([FACTOR_SPECS[k] for k in sorted(FACTOR_SPECS)])


def candidate_grid(universes=("all_liquid", "main_liquid", "liquid1000"),
                   top_ns=(20, 50, 100), frequencies=("weekly", "monthly"),
                   factor_ids=None):
    """78 specifications x 3 pools x 3 sizes x 2 schedules = 1,404 candidates.

    Universe eligibility is the runner's causal mask; pool IDs intentionally
    match research.UNIVERSES. Counts describe configurations, not ideas.
    """
    ids = sorted(FACTOR_SPECS if factor_ids is None else factor_ids)
    if len(set(ids)) != len(ids) or any(k not in FACTOR_SPECS for k in ids):
        raise ValueError("Unknown or duplicate factor IDs")
    if any(type(n) is not int or n <= 0 for n in top_ns):
        raise ValueError("Hold counts must be positive integers")
    if any(f not in ("daily", "weekly", "monthly") for f in frequencies):
        raise ValueError("Unsupported rebalance frequency")
    out = [{"experiment_id": f"{fid}__{pool}__n{n}__{freq}",
            "factor": fid, "name": FACTOR_SPECS[fid]["name"],
            "universe": pool, "top_n": n, "rebalance": freq}
           for fid in ids for pool in universes for n in top_ns for freq in frequencies]
    if len({x["experiment_id"] for x in out}) != len(out):
        raise ValueError("Duplicate grid axes")
    return out


def _divide(a, b):
    a, b = np.broadcast_arrays(a, b)
    out = np.full(a.shape, np.nan, dtype=np.float64)
    np.divide(a, b, out=out, where=np.isfinite(a) & np.isfinite(b) & (b > 0))
    return out


def _lag(x, n):
    out = np.full_like(x, np.nan, dtype=np.float64)
    if n < len(x):
        out[n:] = x[:-n]
    return out


def _rolling(x, w, method="mean"):
    roll = pd.DataFrame(x).rolling(w, min_periods=w)
    if method == "std":
        return roll.std(ddof=1).to_numpy()
    if method == "quantile":
        return roll.quantile(.1, interpolation="linear").to_numpy()
    return getattr(roll, method)().to_numpy()


def _complete(x, w):
    return _rolling(np.isfinite(x).astype(float), w, "sum") == w


class FactorEngine:
    """Reusable primitive arrays plus at most max_cache signed score matrices.

    No state is shared with the simulator. compute does not rank across stocks;
    score ranks only within its supplied final eligible universe. No factor
    currently requires a cross-sectional intermediate or universe dependency.
    """
    def __init__(self, market, max_cache=1):
        if type(max_cache) is not int or max_cache < 0 or max_cache > 2:
            raise ValueError("max_cache must be 0, 1, or 2 matrices")
        self.market = market
        self.raw = market.raw
        if any(k not in self.raw for k in SOURCE_FIELDS):
            raise ValueError("V4 requires all seven source matrices")
        self.shape = np.asarray(self.raw["close"]).shape
        if len(self.shape) != 2 or any(np.asarray(self.raw[k]).shape != self.shape for k in SOURCE_FIELDS):
            raise ValueError("All source fields must have the same [date, symbol] shape")
        if not all(self.shape):
            raise ValueError("Empty source matrix")
        self.max_cache = max_cache
        self._cache = OrderedDict()
        self._primitives = {}

    def clear(self):
        """Release every cached result and primitive; do not mutate market."""
        self._cache.clear()
        self._primitives.clear()

    def _get(self, key):
        if key in self._primitives:
            return self._primitives[key]
        if key == "scale":
            f = np.asarray(self.raw["cumadj"], dtype=float)
            valid = np.isfinite(f) & (f > 0)
            first = valid.argmax(axis=0)
            base = f[first, np.arange(self.shape[1])]
            x = _divide(np.where(valid, f, np.nan), base)
        elif key in ("C", "O", "H", "L"):
            name = {"C": "close", "O": "open", "H": "high", "L": "low"}[key]
            p = np.asarray(self.raw[name], dtype=float)
            x = np.where(np.isfinite(p) & (p > 0), p * self._get("scale"), np.nan)
        elif key in ("A", "N"):
            p = np.asarray(self.raw["amt" if key == "A" else "num_trades"], dtype=float)
            x = np.where(np.isfinite(p) & (p >= 0), p, np.nan)
        elif key == "r":
            x = _divide(self._get("C"), _lag(self._get("C"), 1)) - 1
        elif key == "intraday":
            x = _divide(self._get("C"), self._get("O")) - 1
        elif key == "gap":
            x = _divide(self._get("O"), _lag(self._get("C"), 1)) - 1
        elif key in ("range", "position"):
            h, l, c = self._get("H"), self._get("L"), self._get("C")
            valid = np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (h >= c) & (c >= l)
            x = _divide(h-l, c) if key == "range" else _divide(2*c-h-l, h-l)
            x = np.where(valid, x, np.nan)
        else:
            raise KeyError(key)
        x = np.where(np.isfinite(x), x, np.nan)
        self._primitives[key] = x
        return x

    def compute(self, factor_id, eligible=None):
        """Signed unranked score; optional eligibility only masks the result.

        Cached values are read-only to prevent a caller changing later runs.
        An optional eligibility mask never affects the underlying cached score.
        """
        if factor_id not in FACTOR_SPECS:
            raise KeyError(factor_id)
        if factor_id in self._cache:
            self._cache.move_to_end(factor_id)
            out = self._cache[factor_id]
        else:
            spec = FACTOR_SPECS[factor_id]
            with np.errstate(all="ignore"):
                natural = self._compute(spec)
            out = np.asarray(natural * spec["direction"], dtype=np.float64)
            out[~np.isfinite(out)] = np.nan
            out[:spec["min_history"] - 1] = np.nan
            out.setflags(write=False)
            if self.max_cache:
                self._cache[factor_id] = out
                while len(self._cache) > self.max_cache:
                    self._cache.popitem(last=False)
        if eligible is not None:
            mask = np.asarray(eligible, dtype=bool)
            if mask.shape != self.shape:
                raise ValueError("Eligibility shape mismatch")
            return np.where(mask, out, np.nan)
        return out

    def score(self, factor_id, eligible, rank=True):
        """Average percentile rank on final eligible finite names, >=2/day."""
        x = self.compute(factor_id, eligible)
        if not rank:
            return x
        enough = np.sum(np.isfinite(x), axis=1) >= 2
        out = pd.DataFrame(x).rank(axis=1, method="average", pct=True).to_numpy()
        out[~enough] = np.nan
        return out

    def _compute(self, spec):
        idea, w = spec["idea_id"], spec["window"]
        if idea in ("reversal", "momentum", "momentum_skip5"):
            c = self._get("C")
            skip = 5 if idea == "momentum_skip5" else 0
            out = _divide(_lag(c, skip) if skip else c, _lag(c, w+skip)) - 1
            # Require the complete endpoint-to-endpoint price path; deliberately
            # also require the skipped five rows, instead of bridging missing data.
            return np.where(_complete(c, w+skip+1), out, np.nan)
        if idea in ("trend_efficiency", "return_to_risk", "lowvol", "downside_risk", "tail_return", "positive_fraction"):
            r = self._get("r")
            if idea == "trend_efficiency":
                c = self._get("C")
                return _divide(_divide(c, _lag(c, w))-1, _rolling(np.abs(r), w, "sum"))
            if idea == "return_to_risk":
                return _divide(_rolling(r, w), _rolling(r, w, "std"))
            if idea == "lowvol":
                return _rolling(r, w, "std")
            if idea == "downside_risk":
                return np.sqrt(_rolling(np.minimum(r, 0)**2, w))
            if idea == "tail_return":
                return _rolling(r, w, "quantile")
            return _rolling(np.where(np.isfinite(r), (r > 0).astype(float), np.nan), w)
        if idea in ("intraday_trend", "overnight_reversal", "gap_volatility", "intraday_volatility"):
            x = self._get("gap" if idea in ("overnight_reversal", "gap_volatility") else "intraday")
            return _rolling(x, w, "std" if idea.endswith("volatility") else "mean")
        if idea in ("lowrange", "closing_position", "range_surprise"):
            x = self._get("position" if idea == "closing_position" else "range")
            return _divide(x, _rolling(x, w)) if idea == "range_surprise" else _rolling(x, w)
        if idea == "mean_reversion":
            c = self._get("C")
            return _divide(c, _rolling(c, w))-1
        if idea in ("high_position", "channel_position"):
            c, h = self._get("C"), self._get("H")
            good = np.isfinite(c) & np.isfinite(h) & (h >= c)
            if idea == "high_position":
                return _divide(c, _rolling(np.where(good, h, np.nan), w, "max"))-1
            l = self._get("L")
            good &= np.isfinite(l) & (c >= l)
            hi = _rolling(np.where(good, h, np.nan), w, "max")
            lo = _rolling(np.where(good, l, np.nan), w, "min")
            return _divide(c-lo, hi-lo)
        if idea in ("liquidity", "amount_stability", "amount_trend"):
            a = self._get("A")
            if idea == "liquidity":
                return np.log1p(_rolling(a, w))
            if idea == "amount_stability":
                return _rolling(np.log1p(a), w, "std")
            ratio = _divide(_rolling(a, 5), _rolling(a, w))
            return np.where(ratio > 0, np.log(ratio), np.nan)
        if idea == "amihud":
            return _rolling(_divide(np.abs(self._get("r")), self._get("A") / 1e8), w)
        if idea == "return_amount_corr":
            r, a = self._get("r"), np.log1p(self._get("A"))
            # Explicitly require paired complete observations and positive
            # marginal variances, not pandas' loose pairwise fallback behavior.
            paired = np.isfinite(r) & np.isfinite(a)
            r, a = np.where(paired, r, np.nan), np.where(paired, a, np.nan)
            out = pd.DataFrame(r).rolling(w, min_periods=w).corr(pd.DataFrame(a), ddof=1).to_numpy()
            valid = (_rolling(r, w, "std") > 0) & (_rolling(a, w, "std") > 0) & _complete(r, w)
            return np.where(valid, np.clip(out, -1, 1), np.nan)
        if idea == "trade_intensity":
            return np.log1p(_rolling(self._get("N"), w))
        if idea == "average_trade_value":
            return np.log1p(_rolling(_divide(self._get("A"), self._get("N")), w))
        raise KeyError(idea)


def compute_factor(market, factor_id, eligible=None):
    return FactorEngine(market, max_cache=0).compute(factor_id, eligible)
