"""Typed, causal JSON strategy blocks. No Python execution or dynamic imports.

Numeric missing values propagate. Comparisons/logical operations use strict
missing propagation; missing final filter values exclude the observation.
Cross-sectional score operations see only base eligibility AND final filter.
"""
import copy
import json
import math
import re
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd

MAX_DEPTH = 16
MAX_NODES = 128
MAX_EXPANDED_NODES = 128
MAX_WINDOW = 252
MAX_JSON_BYTES = 150_000
ROOT = Path(__file__).resolve().parent
CATEGORIES = [
    {"id": "data", "label": "行情数据"},
    {"id": "rolling", "label": "历史与滚动"},
    {"id": "arithmetic", "label": "数值运算"},
    {"id": "cross_section", "label": "横截面"},
    {"id": "comparison", "label": "比较条件"},
    {"id": "logic", "label": "逻辑条件"},
    {"id": "factors", "label": "预制因子"},
]
BLOCKS = {}


def _param(kind, default, low, high):
    return {"type": kind, "default": default, "min": low, "max": high}


WINDOW = _param("integer", 20, 2, MAX_WINDOW)
PERIOD = _param("integer", 5, 1, MAX_WINDOW)


def _add(op, label, category, inputs=(), params=None, output="series", description=None, defaults=None):
    inputs = [{"name": name, "type": kind} for name, kind in inputs]
    params = copy.deepcopy(params or {})
    args = defaults if defaults is not None else [
        {"op": "close", "args": [], "params": {}} if item["type"] == "series"
        else {"op": "is_finite", "args": [{"op": "close", "args": [], "params": {}}], "params": {}}
        for item in inputs]
    BLOCKS[op] = {"id": op, "label": label, "category": category,
                  "description": description or label + "；只使用当前及历史观测，缺失值不填零。",
                  "inputs": inputs, "params": params, "output": output,
                  "default": {"op": op, "args": args, "params": {k: v["default"] for k, v in params.items()}}}


for _op, _label in [
    ("open", "复权开盘价"), ("high", "复权最高价"), ("low", "复权最低价"), ("close", "复权收盘价"),
    ("raw_open", "原始开盘价"), ("raw_high", "原始最高价"), ("raw_low", "原始最低价"), ("raw_close", "原始收盘价"),
    ("amount", "成交金额"), ("adjustment_factor", "原始累计复权因子"), ("trade_count", "成交笔数"),
    ("history", "已有收盘观测天数"), ("daily_return", "复权日收益率"), ("adjustment_scale", "归一化复权倍数")]:
    _add(_op, _label, "data", description=_label + "；trade_count若源数据未提供则全为缺失，绝不填造。" if _op == "trade_count" else None)

_add("constant", "数值常量", "arithmetic", params={"value": _param("number", 1., -1e9, 1e9)})
for _op, _label in [("add", "相加"), ("subtract", "相减"), ("multiply", "相乘"), ("divide", "相除"),
                    ("power", "乘方"), ("minimum", "逐点较小值"), ("maximum", "逐点较大值")]:
    _add(_op, _label, "arithmetic", [("x", "series"), ("y", "series")])
for _op, _label in [("abs", "绝对值"), ("negate", "取相反数"), ("log", "自然对数"), ("log1p", "log(1+x)"),
                    ("sqrt", "平方根"), ("sign", "符号"), ("reciprocal", "倒数"), ("square", "平方")]:
    _add(_op, _label, "arithmetic", [("x", "series")])
_add("clip", "数值截断", "arithmetic", [("x", "series")],
     {"lower": _param("number", -3., -1e9, 1e9), "upper": _param("number", 3., -1e9, 1e9)})
_add("where", "按条件选择数值", "arithmetic", [("condition", "boolean"), ("yes", "series"), ("no", "series")])

_add("lag", "历史滞后值", "rolling", [("x", "series")], {"period": _param("integer", 5, 0, MAX_WINDOW)})
for _op, _label in [("delta", "历史差值"), ("pct_change", "历史变化率")]:
    _add(_op, _label, "rolling", [("x", "series")], {"period": PERIOD})
for _op, _label in [("rolling_mean", "滚动均值"), ("rolling_sum", "滚动求和"), ("rolling_std", "滚动标准差"),
                    ("rolling_var", "滚动方差"), ("rolling_min", "滚动最小值"), ("rolling_max", "滚动最大值"),
                    ("rolling_median", "滚动中位数"), ("rolling_rank", "窗口内当前值百分位"),
                    ("rolling_zscore", "窗口内当前值标准分"), ("rolling_skew", "滚动偏度"),
                    ("rolling_kurt", "滚动超额峰度"), ("rolling_argmax", "窗口最大值距今天数"),
                    ("rolling_argmin", "窗口最小值距今天数"), ("linear_decay", "线性加权均值"),
                    ("ema", "指数加权均值")]:
    _add(_op, _label, "rolling", [("x", "series")], {"window": WINDOW},
         description=_label + "；min_periods=window，标准差/方差ddof=1；窗口缺失不填零。EMA使用截至当日全部历史，span=window、adjust=False。" if _op == "ema" else None)
_add("rolling_quantile", "滚动分位数", "rolling", [("x", "series")], {"window": WINDOW, "q": _param("number", .5, 0., 1.)})
for _op, _label in [("rolling_corr", "滚动相关系数"), ("rolling_cov", "滚动协方差")]:
    _add(_op, _label, "rolling", [("x", "series"), ("y", "series")], {"window": WINDOW})

for _op, _label in [("cs_rank", "横截面百分位排名"), ("cs_zscore", "横截面标准分"),
                    ("cs_demean", "横截面去均值"), ("cs_scale", "横截面绝对值归一"), ("cs_median_distance", "偏离横截面中位数")]:
    _add(_op, _label, "cross_section", [("x", "series")])
_add("cs_winsorize", "横截面分位截尾", "cross_section", [("x", "series")], {"tail": _param("number", .05, 0., .49)})

for _op, _label in [("greater", "大于"), ("greater_equal", "大于等于"), ("less", "小于"),
                    ("less_equal", "小于等于"), ("equal", "等于"), ("not_equal", "不等于")]:
    _add(_op, _label, "comparison", [("x", "series"), ("y", "series")], output="boolean")
for _op, _label in [("is_finite", "数值有效"), ("is_missing", "数值缺失")]:
    _add(_op, _label, "comparison", [("x", "series")], output="boolean")
_add("between", "落在闭区间内", "comparison", [("x", "series")],
     {"lower": _param("number", 0., -1e9, 1e9), "upper": _param("number", 1., -1e9, 1e9)}, "boolean")
for _op, _label in [("and", "条件同时成立"), ("or", "条件任一成立"), ("xor", "条件恰有一个成立")]:
    _add(_op, _label, "logic", [("x", "boolean"), ("y", "boolean")], output="boolean")
_add("not", "条件取反", "logic", [("x", "boolean")], output="boolean")

for _op, _label, _desc in [
    ("factor_momentum60", "60日动量跳过5日", "close[t-5]/close[t-65]-1；越高越优。"),
    ("factor_reversal5", "5日反转", "-(close[t]/close[t-5]-1)；越高越优。"),
    ("factor_lowvol20", "20日低波动", "负的20日复权收益率样本标准差，ddof=1。"),
    ("factor_liquidity20", "20日成交额因子", "log1p(20日成交额均值)。"),
    ("factor_combo", "固定四因子等权", "四因子在最终可选股票范围内百分位排名的等权均值；任一缺失则缺失。"),
    ("factor_intraday_return", "日内复权收益", "close/open-1；当天收盘计算，最早次日执行。"),
    ("factor_range", "日内振幅", "(high-low)/close；使用同一复权比例，最早次日执行。")]:
    _add(_op, _label, "factors", description=_desc)


def catalog():
    return {"format": "daily-factor-block-catalog", "version": 1, "categories": copy.deepcopy(CATEGORIES),
            "blocks": copy.deepcopy(list(BLOCKS.values())),
            "limits": {"max_depth": MAX_DEPTH, "max_nodes": MAX_NODES, "max_expanded_nodes": MAX_EXPANDED_NODES,
                       "max_window": MAX_WINDOW, "max_json_bytes": MAX_JSON_BYTES},
            "semantics": {"missing_numeric": "Preserve NaN; never fill missing numeric values with zero.",
                          "missing_boolean": "Comparisons and logic propagate missing; final filter missing is false.",
                          "cross_section": "Score cross sections use base eligibility intersected with the evaluated filter.",
                          "rolling": "Trailing windows only; min_periods=window; std/var/cov ddof=1."}}


class StrategyError(ValueError):
    pass


_FORBIDDEN_KEYS = {"__proto__", "prototype", "constructor", "eval", "exec", "code", "python", "script", "callable", "globals", "locals", "__import__"}


def _json_guard(value):
    seen = set()
    def visit(x, depth):
        if depth > 80:
            raise StrategyError("JSON nesting is too deep")
        if type(x) in (dict, list):
            if id(x) in seen:
                raise StrategyError("JSON contains cycles")
            seen.add(id(x))
            if type(x) is dict:
                for key, item in x.items():
                    if type(key) is not str or key.lower() in _FORBIDDEN_KEYS or "__" in key:
                        raise StrategyError("Executable or unsafe JSON key")
                    visit(item, depth + 1)
            else:
                for item in x:
                    visit(item, depth + 1)
            seen.remove(id(x))
        elif type(x) is float:
            if not math.isfinite(x):
                raise StrategyError("Non-finite JSON number")
        elif type(x) not in (str, int, bool, type(None)):
            raise StrategyError("Only plain JSON data is accepted")
    visit(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, OverflowError, RecursionError) as error:
        raise StrategyError("Invalid JSON data") from error
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise StrategyError("JSON document is too large")


def _keys(obj, required, optional=()):
    if type(obj) is not dict or not set(required) <= set(obj) or set(obj) - set(required) - set(optional):
        raise StrategyError("Missing or unknown fields: expected " + ", ".join(required))


def _name(value, label="name", maximum=120):
    if type(value) is not str or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise StrategyError("Invalid " + label)
    return value.strip()


def _packs(packs):
    if type(packs) is not list or len(packs) > 16:
        raise StrategyError("At most 16 block packs are allowed")
    normalized, custom, pack_ids = [], {}, set()
    for pack in packs:
        _keys(pack, ["format", "version", "id", "name", "blocks"])
        if pack["format"] != "daily-factor-block-pack" or type(pack["version"]) is not int or pack["version"] != 1:
            raise StrategyError("Unsupported block pack format or version")
        pack_id = _name(pack["id"], "pack id", 64)
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", pack_id) or pack_id in pack_ids:
            raise StrategyError("Invalid or duplicate block pack id")
        pack_ids.add(pack_id)
        if type(pack["blocks"]) is not list or not 1 <= len(pack["blocks"]) <= 32:
            raise StrategyError("A block pack needs 1 to 32 blocks")
        blocks = []
        for block in pack["blocks"]:
            _keys(block, ["id", "name", "inputs", "template"])
            block_id = _name(block["id"], "block id", 80)
            if not re.fullmatch(r"custom\.[a-zA-Z0-9][a-zA-Z0-9_.-]*", block_id) or block_id in custom:
                raise StrategyError("Invalid or duplicate custom block id")
            if type(block["inputs"]) is not list or len(block["inputs"]) > 8:
                raise StrategyError("At most 8 typed block inputs are allowed")
            inputs, names = [], set()
            for item in block["inputs"]:
                _keys(item, ["name", "type"])
                name = _name(item["name"], "input name", 40)
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name) or name in names or item["type"] not in ("series", "boolean"):
                    raise StrategyError("Invalid or duplicate typed input")
                names.add(name)
                inputs.append({"name": name, "type": item["type"]})
            normalized_block = {"id": block_id, "name": _name(block["name"]), "inputs": inputs, "template": block["template"]}
            custom[block_id] = normalized_block
            blocks.append(normalized_block)
        normalized.append({"format": "daily-factor-block-pack", "version": 1, "id": pack_id, "name": _name(pack["name"]), "blocks": blocks})
    if len(custom) > 64:
        raise StrategyError("At most 64 custom blocks are allowed")
    return normalized, custom


def _normalize_node(node, custom, input_types, stack, budget, depth=0):
    budget[0] += 1
    if budget[0] > MAX_NODES or depth > MAX_DEPTH:
        raise StrategyError("Expression exceeds node or depth limit")
    _keys(node, ["op"], ["args", "params"])
    op = node["op"]
    if type(op) is not str:
        raise StrategyError("Operation id must be a string")
    args, params = node.get("args", []), node.get("params", {})
    if type(args) is not list or type(params) is not dict:
        raise StrategyError("args must be an array and params an object")
    if op == "input":
        if args or set(params) != {"name"} or type(params["name"]) is not str or params["name"] not in input_types:
            raise StrategyError("Unknown or misplaced custom input")
        return {"op": "input", "args": [], "params": {"name": params["name"]}}, input_types[params["name"]]
    if op in custom:
        block = custom[op]
        if params or len(args) != len(block["inputs"]):
            raise StrategyError("Custom block arity or parameters are invalid")
        if op in stack:
            raise StrategyError("Custom block dependency cycle")
        normalized_args = []
        for arg, spec in zip(args, block["inputs"]):
            norm, kind = _normalize_node(arg, custom, input_types, stack, budget, depth + 1)
            if kind != spec["type"]:
                raise StrategyError("Custom block input type mismatch")
            normalized_args.append(norm)
        _, result_type = _normalize_node(block["template"], custom, {i["name"]: i["type"] for i in block["inputs"]}, stack + [op], budget, depth + 1)
        return {"op": op, "args": normalized_args, "params": {}}, result_type
    if op not in BLOCKS:
        raise StrategyError("Unknown operation: " + op)
    spec = BLOCKS[op]
    if len(args) != len(spec["inputs"]) or set(params) - set(spec["params"]):
        raise StrategyError("Wrong argument count or unknown parameters for " + op)
    normalized_args = []
    for arg, field in zip(args, spec["inputs"]):
        norm, kind = _normalize_node(arg, custom, input_types, stack, budget, depth + 1)
        if kind != field["type"]:
            raise StrategyError("Input type mismatch for " + op)
        normalized_args.append(norm)
    normalized_params = {}
    for key, field in spec["params"].items():
        value = params.get(key, field["default"])
        if isinstance(value, bool) or type(value) not in (float, int):
            raise StrategyError("Parameter must be finite numeric: " + key)
        if field["type"] == "integer" and (type(value) is not int):
            raise StrategyError("Parameter must be an integer: " + key)
        if not field["min"] <= value <= field["max"]:
            raise StrategyError("Parameter out of range: " + key)
        if not math.isfinite(value):
            raise StrategyError("Parameter must be finite numeric: " + key)
        normalized_params[key] = value
    if "lower" in normalized_params and normalized_params["lower"] > normalized_params["upper"]:
        raise StrategyError("lower must not exceed upper")
    return {"op": op, "args": normalized_args, "params": normalized_params}, spec["output"]


def _expand(node, custom, inputs=None, stack=(), budget=None, depth=0):
    budget = [0] if budget is None else budget
    budget[0] += 1
    if budget[0] > MAX_EXPANDED_NODES or depth > MAX_DEPTH:
        raise StrategyError("Expanded custom expression exceeds node or depth limit")
    if node["op"] == "input":
        return _expand(inputs[node["params"]["name"]], custom, None, stack, budget, depth + 1)
    if node["op"] in custom:
        op = node["op"]
        if op in stack:
            raise StrategyError("Custom block dependency cycle")
        args = [_expand(arg, custom, inputs, stack, budget, depth + 1) for arg in node.get("args", [])]
        block = custom[op]
        return _expand(block["template"], custom, dict(zip([i["name"] for i in block["inputs"]], args)), stack + (op,), budget, depth + 1)
    return {"op": node["op"], "args": [_expand(arg, custom, inputs, stack, budget, depth + 1) for arg in node.get("args", [])],
            "params": copy.deepcopy(node.get("params", {}))}


def validate_strategy(document):
    """Return {'document': normalized JSON, 'meta': ..., 'expanded': ...}.

    Static validation requires no Market and does not execute expressions.
    """
    _json_guard(document)
    _keys(document, ["format", "version", "name", "score"], ["filter", "packs", "selection"])
    if document["format"] != "daily-factor-strategy" or type(document["version"]) is not int or document["version"] != 1:
        raise StrategyError("Unsupported strategy format or version")
    packs, custom = _packs(document.get("packs", []))
    selection = document.get("selection", {})
    _keys(selection, [], ["top_n", "rebalance", "direction"])
    if "top_n" in selection and (type(selection["top_n"]) is not int or not 1 <= selection["top_n"] <= 500):
        raise StrategyError("top_n must be an integer from 1 to 500")
    if selection.get("rebalance", "weekly") not in ("daily", "weekly", "monthly") or selection.get("direction", "desc") not in ("asc", "desc"):
        raise StrategyError("Invalid selection direction or rebalance frequency")
    selection = dict(selection)
    selection.setdefault("direction", "desc")
    # Validate even unused imported templates; hidden malicious macros are rejected.
    for block in custom.values():
        norm, kind = _normalize_node(block["template"], custom, {x["name"]: x["type"] for x in block["inputs"]}, [block["id"]], [0])
        block["template"] = norm
        block["output"] = kind
    for block in custom.values():
        dummy = [{"op": "close", "args": [], "params": {}} if item["type"] == "series"
                 else {"op": "is_finite", "args": [{"op": "close", "args": [], "params": {}}], "params": {}}
                 for item in block["inputs"]]
        _expand({"op": block["id"], "args": dummy, "params": {}}, custom)
    budget = [0]
    score, kind = _normalize_node(document["score"], custom, {}, [], budget)
    if kind != "series":
        raise StrategyError("score must produce a numeric series")
    normalized = {"format": "daily-factor-strategy", "version": 1, "name": _name(document["name"]),
                  "score": score, "selection": copy.deepcopy(selection), "packs": packs}
    expanded = {"score": _expand(score, custom)}
    if "filter" in document:
        filt, kind = _normalize_node(document["filter"], custom, {}, [], budget)
        if kind != "boolean":
            raise StrategyError("filter must produce a boolean series")
        normalized["filter"] = filt
        expanded["filter"] = _expand(filt, custom)
    # output is metadata, not part of strict import schema.
    for pack in packs:
        for block in pack["blocks"]:
            block.pop("output", None)
    operations = set()
    def collect(node):
        operations.add(node["op"])
        for arg in node["args"]:
            collect(arg)
    for node in expanded.values():
        collect(node)
    return {"document": normalized, "strategy": normalized, "expanded": expanded,
            "meta": {"node_count": budget[0], "operations": sorted(operations), "custom_blocks": len(custom),
                     "selection": copy.deepcopy(selection), "causal": True,
                     "missing_boolean_policy": "strict propagation; missing final filter excluded"}}


def validate_block_pack(pack):
    """Validate an independently importable pack, including unused definitions."""
    wrapper = {"format": "daily-factor-strategy", "version": 1, "name": "Block pack validation", "score": {"op": "close"},
               "selection": {"top_n": 100, "rebalance": "weekly", "direction": "desc"}, "packs": [pack]}
    valid = validate_strategy(wrapper)
    return {"pack": valid["document"]["packs"][0], "meta": valid["meta"]}


def get_catalog(packs=None):
    """Catalog plus validated custom blocks with executable default arguments."""
    result = catalog()
    if packs:
        wrapper = {"format": "daily-factor-strategy", "version": 1, "name": "Catalog validation",
                   "score": {"op": "close"}, "packs": packs, "selection": {}}
        valid = validate_strategy(wrapper)
        for pack in valid["document"]["packs"]:
            for block in pack["blocks"]:
                invocation = {"op": block["id"], "args": [
                    {"op": "close"} if item["type"] == "series" else {"op": "is_finite", "args": [{"op": "close"}]}
                    for item in block["inputs"]], "params": {}}
                custom = {b["id"]: b for p in valid["document"]["packs"] for b in p["blocks"]}
                _, kind = _normalize_node(invocation, custom, {}, [], [0])
                result["blocks"].append({"id": block["id"], "label": block["name"], "category": "custom",
                                         "description": "白名单积木组合；来源积木包：" + pack["name"],
                                         "inputs": copy.deepcopy(block["inputs"]), "params": {}, "output": kind,
                                         "default": invocation, "pack_id": pack["id"]})
        result["categories"].append({"id": "custom", "label": "自定义积木"})
    return result


def _safe(array):
    array = np.asarray(array, dtype=float)
    return np.where(np.isfinite(array), array, np.nan)


def _evaluate(node, market, universe, cache=None):
    cache = OrderedDict() if cache is None else cache
    key = json.dumps(node, sort_keys=True, separators=(",", ":"))
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    value = _evaluate_uncached(node, market, universe, cache)
    cache[key] = value
    # At most eight cached matrices (about 576 MB on the full source shape),
    # shared by identical subexpressions; recursion depth is separately bounded.
    while len(cache) > 8:
        cache.popitem(last=False)
    return value


def _evaluate_uncached(node, market, universe, cache):
    op, p = node["op"], node["params"]
    shape = market.close.shape
    if op in ("open", "close"):
        return _safe(getattr(market, op))
    if op in ("high", "low"):
        return _safe(market.raw[op] * market.scale)
    if op.startswith("raw_"):
        return _safe(market.raw[op[4:]])
    if op in ("amount", "adjustment_factor", "trade_count"):
        key = {"amount": "amt", "adjustment_factor": "cumadj", "trade_count": "num_trades"}[op]
        return _safe(market.raw[key]) if key in market.raw else np.full(shape, np.nan)
    if op in ("daily_return", "history", "adjustment_scale"):
        return _safe(getattr(market, {"daily_return": "returns", "history": "history", "adjustment_scale": "scale"}[op]))
    if op == "constant":
        return np.full(shape, p["value"], dtype=float)
    if op.startswith("factor_"):
        close = pd.DataFrame(market.close)
        values = {
            "factor_momentum60": lambda: (close.shift(5) / close.shift(65) - 1).to_numpy(),
            "factor_reversal5": lambda: (-(close / close.shift(5) - 1)).to_numpy(),
            "factor_lowvol20": lambda: -pd.DataFrame(market.returns).rolling(20, min_periods=20).std(ddof=1).to_numpy(),
            "factor_liquidity20": lambda: np.log1p(pd.DataFrame(market.raw["amt"]).rolling(20, min_periods=20).mean().to_numpy()),
            "factor_intraday_return": lambda: market.close / market.open - 1,
            "factor_range": lambda: (market.raw["high"] - market.raw["low"]) * market.scale / market.close,
        }
        if op == "factor_combo":
            ranks = [_evaluate({"op": "cs_rank", "args": [{"op": name, "args": [], "params": {}}], "params": {}}, market, universe, cache)
                     for name in ("factor_momentum60", "factor_reversal5", "factor_lowvol20", "factor_liquidity20")]
            return _safe(np.mean(ranks, axis=0))
        return _safe(values[op]())
    args = [_evaluate(arg, market, universe, cache) for arg in node["args"]]
    x = args[0]
    with np.errstate(all="ignore"):
        binary = {"add": np.add, "subtract": np.subtract, "multiply": np.multiply, "divide": np.divide,
                  "power": np.power, "minimum": np.minimum, "maximum": np.maximum}
        unary = {"abs": np.abs, "negate": np.negative, "log": np.log, "log1p": np.log1p,
                 "sqrt": np.sqrt, "sign": np.sign, "reciprocal": np.reciprocal, "square": np.square}
        if op in binary:
            return _safe(np.where(np.isfinite(x) & np.isfinite(args[1]), binary[op](x, args[1]), np.nan))
        if op in unary:
            return _safe(unary[op](x))
        if op == "clip":
            return _safe(np.clip(x, p["lower"], p["upper"]))
        if op == "where":
            return _safe(np.where(np.isfinite(x), np.where(x != 0, args[1], args[2]), np.nan))
        if op in ("lag", "delta", "pct_change"):
            lag = pd.DataFrame(x).shift(p["period"]).to_numpy()
            return _safe(lag if op == "lag" else (x - lag if op == "delta" else x / lag - 1))
        if op.startswith("rolling_") or op in ("linear_decay", "ema"):
            frame = pd.DataFrame(x)
            window = p["window"]
            rolling = frame.rolling(window, min_periods=window)
            method = op.removeprefix("rolling_")
            if method in ("mean", "sum", "min", "max", "median"):
                out = getattr(rolling, method)()
            elif method in ("skew", "kurt"):
                # pandas rolling skew/kurt internally center on a full-column
                # statistic, which lets future changes perturb old floats.
                # Center on the first historical valid value, then use only
                # trailing raw moments with unbiased finite-sample corrections.
                valid = np.isfinite(x)
                first = valid.argmax(axis=0)
                offset = x[first, np.arange(x.shape[1])]
                centered = frame - offset
                mean = centered.rolling(window, min_periods=window).mean()
                second = centered.pow(2).rolling(window, min_periods=window).mean()
                third = centered.pow(3).rolling(window, min_periods=window).mean()
                m2 = (second - mean.pow(2)).where(lambda a: a > 0)
                m3 = third - 3 * mean * second + 2 * mean.pow(3)
                if method == "skew":
                    out = math.sqrt(window * (window - 1)) / (window - 2) * m3 / m2.pow(1.5) if window > 2 else frame * np.nan
                else:
                    fourth = centered.pow(4).rolling(window, min_periods=window).mean()
                    m4 = fourth - 4 * mean * third + 6 * mean.pow(2) * second - 3 * mean.pow(4)
                    out = (window - 1) / ((window - 2) * (window - 3)) * ((window + 1) * (m4 / m2.pow(2) - 3) + 6) if window > 3 else frame * np.nan
            elif method in ("std", "var"):
                out = getattr(rolling, method)(ddof=1)
            elif method == "quantile":
                out = rolling.quantile(p["q"])
            elif method == "rank":
                out = rolling.rank(method="average", pct=True)
            elif method == "zscore":
                out = (frame - rolling.mean()) / rolling.std(ddof=1)
            elif method == "corr":
                out = rolling.corr(pd.DataFrame(args[1]), ddof=1)
            elif method == "cov":
                out = rolling.cov(pd.DataFrame(args[1]), ddof=1)
            elif method in ("argmax", "argmin"):
                func = np.argmax if method == "argmax" else np.argmin
                out = rolling.apply(lambda a: func(a[::-1]), raw=True)
            elif op == "linear_decay":
                weights = np.arange(1, window + 1, dtype=float)
                out = rolling.apply(lambda a: a @ weights / weights.sum(), raw=True)
            elif op == "ema":
                out = frame.ewm(span=window, min_periods=window, adjust=False, ignore_na=False).mean()
                # A missing current quote stays missing instead of carrying EMA forward.
                out = out.where(frame.notna())
            else:
                raise StrategyError("Unimplemented rolling operation")
            return _safe(out.to_numpy())
        if op.startswith("cs_"):
            frame = pd.DataFrame(np.where(universe, x, np.nan))
            if op == "cs_rank":
                out = frame.rank(axis=1, method="average", pct=True)
            elif op == "cs_demean":
                out = frame.sub(frame.mean(axis=1), axis=0)
            elif op == "cs_zscore":
                out = frame.sub(frame.mean(axis=1), axis=0).div(frame.std(axis=1, ddof=1), axis=0)
            elif op == "cs_scale":
                out = frame.div(frame.abs().sum(axis=1).replace(0, np.nan), axis=0)
            elif op == "cs_median_distance":
                out = frame.sub(frame.median(axis=1), axis=0)
            else:
                out = frame.clip(lower=frame.quantile(p["tail"], axis=1), upper=frame.quantile(1 - p["tail"], axis=1), axis=0)
            return _safe(out.to_numpy())
        if op in ("is_finite", "is_missing"):
            return np.isfinite(x).astype(float) if op == "is_finite" else (~np.isfinite(x)).astype(float)
        compare = {"greater": np.greater, "greater_equal": np.greater_equal, "less": np.less,
                   "less_equal": np.less_equal, "equal": np.equal, "not_equal": np.not_equal}
        if op in compare:
            y = args[1]
            return np.where(np.isfinite(x) & np.isfinite(y), compare[op](x, y).astype(float), np.nan)
        if op == "between":
            return np.where(np.isfinite(x), ((x >= p["lower"]) & (x <= p["upper"])).astype(float), np.nan)
        if op == "not":
            return np.where(np.isfinite(x), (x == 0).astype(float), np.nan)
        if op in ("and", "or", "xor"):
            func = {"and": np.logical_and, "or": np.logical_or, "xor": np.logical_xor}[op]
            return np.where(np.isfinite(x) & np.isfinite(args[1]), func(x != 0, args[1] != 0).astype(float), np.nan)
    raise StrategyError("Unimplemented operation: " + op)


def compile_strategy(market, cfg, document):
    validated = validate_strategy(document)
    validated["document"]["selection"].setdefault("top_n", cfg.top_n)
    validated["document"]["selection"].setdefault("rebalance", cfg.rebalance)
    base = np.asarray(market.eligible(cfg), dtype=bool)
    if base.shape != market.close.shape or base.ndim != 2:
        raise StrategyError("Market arrays must be matching date-by-symbol matrices")
    eligible = base.copy()
    if "filter" in validated["expanded"]:
        filt = _evaluate(validated["expanded"]["filter"], market, base)
        eligible &= np.isfinite(filt) & (filt != 0)
    score = _evaluate(validated["expanded"]["score"], market, eligible)
    score = np.where(eligible & np.isfinite(score), score, np.nan)
    if validated["document"]["selection"]["direction"] == "asc":
        score = -score
    meta = dict(validated["meta"])
    meta["selection"] = copy.deepcopy(validated["document"]["selection"])
    meta.update({"eligible_observations": int(eligible.sum()), "finite_score_observations": int(np.isfinite(score).sum()),
                 "filter_cross_section_universe": "base eligibility", "score_cross_section_universe": "base eligibility AND final filter",
                 "execution_timing": "At close t signal, earliest execution at open t+1 by the research engine"})
    return {"score": score, "eligible": eligible, "meta": meta, "document": validated["document"]}


def export_strategy(document):
    return json.dumps(validate_strategy(document)["document"], ensure_ascii=False, indent=2, allow_nan=False)


def import_strategy(text):
    if type(text) is not str or len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise StrategyError("Strategy JSON text is too large or invalid")
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise StrategyError("Invalid strategy JSON") from error
    return validate_strategy(document)


def export_block_pack(pack):
    return json.dumps(validate_block_pack(pack)["pack"], ensure_ascii=False, indent=2, allow_nan=False)


def import_block_pack(text):
    if type(text) is not str or len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise StrategyError("Block pack JSON text is too large or invalid")
    try:
        pack = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise StrategyError("Invalid block pack JSON") from error
    return validate_block_pack(pack)


if __name__ == "__main__":
    path = ROOT / "block_catalog.json"
    path.write_text(json.dumps(catalog(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(str(path), len(BLOCKS))
