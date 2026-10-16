"""Auditable, retrospective 2024 selection / 2025 evaluation using only feathers.zip.

This is a daily-bar stress model, not proof of executable auction fills. In
particular, the archive contains no true historical ST labels. No external
market data, credential, broker, or model endpoint is used by this module.
"""
import os
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_name] = "1"

import argparse
import ctypes
import hashlib
import json
import math
import time
import traceback
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from lab import ROOT, Config, Market, simulate, performance, ic_analysis

RESEARCH_ROOT = ROOT / "research_outputs"
PERIODS = {
    "selection_2024": ("2024-01-02", "2024-12-31"),
    "evaluation_2025": ("2025-01-02", "2025-12-31"),
    "assignment_2025_2026sep": ("2025-01-02", "2026-09-30"),
}
UNIVERSES = {
    "all_liquid": {"name": "全部A股，20日均额≥2000万元", "universe": "all_a", "liquidity_top_n": 0},
    "main_liquid": {"name": "主板，20日均额≥2000万元", "universe": "main_a", "liquidity_top_n": 0},
    "liquid1000": {"name": "全部A股，过去20日均额前1000", "universe": "all_a", "liquidity_top_n": 1000},
}


def node(op, *args, **params):
    return {"op": op, "args": list(args), "params": params}


_close = node("close")
_dr5 = node("pct_change", _close, period=5)
FACTOR_SPECS = {
    "dr5_momentum": ("5DR正向：5日涨幅高优先", "C_adj[t]/C_adj[t-5]-1", _dr5),
    "dr5_reversal": ("5DR反向：5日跌幅大优先", "-(C_adj[t]/C_adj[t-5]-1)", node("negate", _dr5)),
    "reversal1": ("1日反转", "-r_adj[t]", node("negate", node("daily_return"))),
    "momentum20": ("20日动量", "C_adj[t]/C_adj[t-20]-1", node("pct_change", _close, period=20)),
    "momentum60": ("60日动量跳过5日", "C_adj[t-5]/C_adj[t-65]-1", node("factor_momentum60")),
    "lowvol20": ("20日低波动", "-std(r_adj[t-19:t],ddof=1)", node("factor_lowvol20")),
    "liquidity20": ("20日高成交额", "log1p(mean(amt[t-19:t]))", node("factor_liquidity20")),
    "small_amount20": ("20日低成交额", "-log1p(mean(amt[t-19:t]))", node("negate", node("factor_liquidity20"))),
    "lowrange20": ("20日低振幅", "-mean((high-low)/close,20)", node("negate", node("rolling_mean", node("factor_range"), window=20))),
    "reversal_lowvol": ("反转与低波动等权", "0.5*(rank(-5DR)+rank(-vol20))", node("divide", node("add", node("cs_rank", node("factor_reversal5")), node("cs_rank", node("factor_lowvol20"))), node("constant", value=2))),
}

LIMITATIONS = [
    "仅使用用户指定的feathers.zip。没有股票名称或历史ST字段，真实ST已知覆盖为0；不能证明实现了真实非ST全市场筛选。",
    "price_proxy只按截至信号日的主板过去价格锁板特征排除疑似风险股，存在误判与漏判，不能识别所有ST或创业板/科创板ST；未使用全年度未来名单。strict模式因真实状态未知而拒绝运行。",
    "前日收盘确定目标与原始等价股数，次日开盘仅尝试执行。OHLC和全天成交额提供的是事后执行审计，不能还原集合竞价排队、深度、真实订单延迟或开盘成交量。",
    "理论板块限价和未知ST的保守门禁不等于官方每日限价；公司行为日、停牌和特殊无涨跌幅日均缺官方元数据。",
    "cumadj只支持合成总回报份额；不能分离现金红利、送转股、分红税和到账延迟。账本金额可精确复核，仍不是实际股票交收账本。",
    "10亿元容量以过去20日均额上限及当日最终成交额审计上限双重约束；当日额上限是事后安全情景。无竞价量数据，不能声称已证明10亿元可执行。",
    "课程将amt描述为成交额，但包内元数据没有明确货币单位；资金和成交额均按人民币元建模是一项公开假设，没有外部单位核验。",
    "末日发起清算但仍受跌停、停牌、T+1和容量约束；残仓按收盘估值单列，不假造已全额离场。",
    "这是同一数据源的回顾性研究；此前已看过部分2025所在全区间的V1结果，2025并非从未触碰的独立确认样本。候选按2024选择，2025全候选透明呈现；按2025排序的最佳项只属探索。",
    "多策略、多股票池、多持仓数和频率的探索会产生选择偏差。只报告原始P，不把显著性或单年最佳收益当作未来盈利保证。",
    "日线收盘估值的最大回撤遗漏盘中风险；缺真实ST、公司行为及撮合数据时，不存在能够从这些文件证明的高精度实盘盈利结论。",
]


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def finite_json(value):
    if isinstance(value, dict):
        return {str(k): finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def rows(frame):
    return finite_json(frame.to_dict(orient="records"))


def reserve_performance_cores():
    """Reserve two actual SMT cores using Windows CPU-set topology when available."""
    info = {"method": "single_thread_numeric_libraries", "reserved_logical_processors": []}
    if os.name != "nt":
        return info
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        needed = ctypes.c_ulong()
        kernel.GetSystemCpuSetInformation(None, 0, ctypes.byref(needed), None, 0)
        buffer = ctypes.create_string_buffer(needed.value)
        if not kernel.GetSystemCpuSetInformation(buffer, needed.value, ctypes.byref(needed), None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        raw = buffer.raw
        records, position = [], 0
        while position + 8 <= needed.value:
            size = int.from_bytes(raw[position:position+4], "little")
            kind = int.from_bytes(raw[position+4:position+8], "little")
            if not size:
                break
            if kind == 0 and size >= 24:
                records.append({"logical": raw[position+14], "core": raw[position+15], "efficiency": raw[position+18], "group": int.from_bytes(raw[position+12:position+14], "little")})
            position += size
        group = [r for r in records if r["group"] == 0]
        best_efficiency = max(r["efficiency"] for r in group)
        best = [r for r in group if r["efficiency"] == best_efficiency]
        core_counts = {r["core"]: sum(s["core"] == r["core"] for s in best) for r in best}
        cores = sorted(core_counts, key=lambda c: (-core_counts[c], c))[:2]
        reserved = [r["logical"] for r in group if r["core"] in cores]
        allowed = [r["logical"] for r in group if r["logical"] not in reserved]
        mask = sum(1 << cpu for cpu in allowed)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        if not allowed or not kernel.SetProcessAffinityMask(kernel.GetCurrentProcess(), mask):
            raise ctypes.WinError(ctypes.get_last_error())
        info.update(method="Windows_CPU_set_topology", reserved_logical_processors=reserved, allowed_logical_processors=allowed, cpu_sets=group)
    except Exception as exc:
        # Limited numeric threads remain in force; never change any unrelated process.
        info["topology_error"] = str(exc)
    return info


def base_config(**overrides):
    values = dict(start="2025-01-02", end="2025-12-31", capital=1_000_000_000,
                  top_n=100, rebalance="weekly", min_history=120, min_price=2.,
                  min_avg_amount=20_000_000, min_active_days20=18,
                  commission=.0001, min_commission=5., slippage=.0005,
                  cash_buffer=.01, max_participation=.01, open_guard=.048,
                  writeoff_missing_days=20, execution_model="daily_conservative",
                  st_policy="price_proxy", universe="all_a", liquidity_top_n=0,
                  risk_proxy_window=20, max_daily_participation=.01, liquidate_end=True)
    values.update(overrides)
    return Config.from_dict(values)


def candidate_grid():
    return [{"experiment_id": f"{factor}__{universe}__n{count}__{freq}",
             "factor": factor, "name": spec[0], "universe": universe,
             "universe_name": UNIVERSES[universe]["name"], "top_n": count, "rebalance": freq}
            for factor, spec in FACTOR_SPECS.items()
            for universe in UNIVERSES for count in (50, 100) for freq in ("weekly", "monthly")]


def candidate_config(candidate, period, **extra):
    universe = UNIVERSES[candidate["universe"]]
    start, end = PERIODS[period]
    return base_config(start=start, end=end, universe=universe["universe"],
                       liquidity_top_n=universe["liquidity_top_n"],
                       top_n=candidate["top_n"], rebalance=candidate["rebalance"], **extra)


def factor_matrices(m):
    c = pd.DataFrame(m.close)
    with np.errstate(invalid="ignore", divide="ignore"):
        dr5 = (c / c.shift(5) - 1).to_numpy()
        range20 = pd.DataFrame((m.raw["high"] - m.raw["low"]) / m.raw["close"]).rolling(20, min_periods=20).mean().to_numpy()
    return {"dr5_momentum": dr5, "dr5_reversal": -dr5, "reversal1": -m.returns,
            "momentum20": (c / c.shift(20) - 1).to_numpy(),
            "momentum60": m.factors["momentum60"], "lowvol20": m.factors["lowvol20"],
            "liquidity20": m.factors["liquidity20"], "small_amount20": -m.factors["liquidity20"],
            "lowrange20": -range20}


def score_for(candidate, eligible, matrices):
    if candidate["factor"] == "reversal_lowvol":
        ranked = [pd.DataFrame(np.where(eligible, matrices[f], np.nan)).rank(axis=1, pct=True, method="average").to_numpy()
                  for f in ("dr5_reversal", "lowvol20")]
        return .5 * (ranked[0] + ranked[1])
    return np.where(eligible, matrices[candidate["factor"]], np.nan)


def split_returns(daily, capital):
    previous = pd.Series(np.r_[capital, daily.nav.to_numpy()[:-1]], index=daily.index)
    output = []
    for label, start, end in (("2025H1", "2025-01-01", "2025-06-30"), ("2025H2", "2025-07-01", "2025-12-31"),
                              ("2026JanSep", "2026-01-01", "2026-09-30")):
        mask = daily.date.between(start, end)
        if mask.any():
            d = daily.loc[mask]
            p = float(previous.loc[d.index[0]])
            metrics = performance(d, p)
            output.append({"period": label, "return": metrics["total_return"], **metrics})
    return output


def persist_simulation(out, experiment_id, role, scenario, result, detailed=False):
    target = out / "runs" / experiment_id / role / scenario
    target.mkdir(parents=True, exist_ok=True)
    result["daily"].to_csv(target / "daily.csv", index=False, encoding="utf-8-sig", float_format="%.17g")
    if detailed:
        for key in ("trades", "holdings", "orders"):
            frame = result.get(key)
            if isinstance(frame, pd.DataFrame):
                frame.to_csv(target / (key + ".csv"), index=False, encoding="utf-8-sig", float_format="%.17g")
    save_json(target / "diagnostics.json", finite_json({k: result.get(k, {}) for k in ("metrics", "blocked", "terminal", "execution_diagnostics")}))
    return target


def experiment_row(candidate, role, scenario, cfg, result):
    terminal = result.get("terminal", {})
    holdings = result["holdings"]
    terminal_value = float(holdings.value.sum()) if "value" in holdings else 0.
    return finite_json({**candidate, "period_role": role, "start": cfg.start, "end": cfg.end,
            "scenario": scenario, **result["metrics"], "blocked_attempts": sum(result["blocked"].values()),
            "terminal_position_value": terminal_value,
            "terminal_positions": len(holdings), "liquidation_complete": terminal_value <= .01,
            "config": asdict(cfg), "terminal": terminal,
            "execution_diagnostics": result.get("execution_diagnostics", {})})


def descriptive_uncertainty(daily, seed=20251016, repetitions=2000, block_length=10):
    """Circular block bootstrap of the fixed portfolio, without reselection.

    This describes path uncertainty under stationarity; it is not a CI for an
    optimized strategy's future profitability and does not correct selection.
    """
    returns = daily["return"].to_numpy(dtype=np.float64)
    count = len(returns)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, count, size=(repetitions, math.ceil(count / block_length)))
    indices = ((starts[:, :, None] + np.arange(block_length)) % count).reshape(repetitions, -1)[:, :count]
    sample = returns[indices]
    paths = np.prod(1 + sample, axis=1) - 1
    return {"method":"circular block bootstrap, portfolio fixed, no model reselection",
            "repetitions":repetitions,"block_length":block_length,"seed":seed,
            "return_ci95_low":float(np.quantile(paths,.025)),"return_ci95_high":float(np.quantile(paths,.975)),
            "caveat":"回顾性同源日路径的平稳假设下描述性区间，不是优化策略的未来收益保证，不消除选型偏差。"}


def choose_on_2024(experiments):
    selection = [r for r in experiments if r["period_role"] == "selection_2024" and r["scenario"] == "base"]
    # Fixed safety screen, then risk-adjusted ranking. Fallback is explicit.
    admissible = [r for r in selection if r["mean_invested"] >= .25 and r["max_drawdown"] >= -.40 and r["writeoff_loss"] == 0]
    candidates = admissible or selection
    if not candidates:
        raise RuntimeError("所有2024选型实验均失败；不可按2025挑选替代")
    ranked = sorted(candidates, key=lambda r: (-r["sharpe"], -r["total_return"], r["experiment_id"]))
    by_factor, factor_fallback = {}, {}
    for factor in FACTOR_SPECS:
        all_factor = [r for r in selection if r["factor"] == factor]
        safe_factor = [r for r in admissible if r["factor"] == factor]
        if all_factor:
            by_factor[factor] = sorted(safe_factor or all_factor, key=lambda r: (-r["sharpe"], -r["total_return"], r["experiment_id"]))[0]
            factor_fallback[factor] = not bool(safe_factor)
    shortlist = [r["experiment_id"] for r in ranked[:3]]
    for factor in ("dr5_momentum", "dr5_reversal", "lowvol20"):
        if factor in by_factor and by_factor[factor]["experiment_id"] not in shortlist:
            shortlist.append(by_factor[factor]["experiment_id"])
    return {"selected_id": ranked[0]["experiment_id"], "shortlist": shortlist,
            "selected_on": "selection_2024", "selection_is_retrospective": True,
            "criterion": "2024平均仓位≥25%、最大回撤不超过40%、无减记；按2024夏普降序、收益降序、ID排序。若无人满足则显式放宽安全筛选，仍仅按2024选择。",
            "safety_screen_fallback": not bool(admissible), "admissible_count": len(admissible),
            "candidate_count": len(selection), "factor_representatives": {f: r["experiment_id"] for f, r in by_factor.items()},
            "factor_safety_screen_fallback":factor_fallback}


def write_report(out, study):
    selected = study["selection"]["selected_id"]
    evaluation = [r for r in study["experiments"] if r["period_role"] == "evaluation_2025" and r["scenario"] == "base"]
    selected_row = next((r for r in evaluation if r["experiment_id"] == selected),None)
    if selected_row is None:
        (out/"report.md").write_text("# 研究未能完成所选策略的2025评价\n\n2024选型保留，但所选策略2025运行失败，不能替换为2025优胜项。查看result.json与failures.json，其余队列结果已保存。\n",encoding="utf-8")
        return
    best = max(evaluation, key=lambda r: r["total_return"])
    lines = ["# 2025选股研究：日线可复核，历史ST状态仍不可核实", "",
             f"研究 `{study['study_id']}`，生成于{study['generated_at']}（Asia/Shanghai）。仅使用课程原始feathers.zip。", "",
             "## 主要结论", "",
             f"- 按2024规则选择的策略为 **{selected_row['name']} / {selected_row['universe_name']} / {selected_row['top_n']}只 / {selected_row['rebalance']}**。2025总收益 **{selected_row['total_return']:.4%}**、最大回撤 **{selected_row['max_drawdown']:.4%}**、夏普 **{selected_row['sharpe']:.4f}**、平均仓位 **{selected_row['mean_invested']:.2%}**。",
             f"- 2025样本内最高收益是 `{best['experiment_id']}`，总收益{best['total_return']:.4%}；这是看过2025结果的探索排序，不替代上述2024选型结果。",
             f"- 主区间2025-01-02至2025-12-31，共{selected_row['days']}个源交易日；初始资金10亿元。首日按2024-12-31收盘信号开始尝试建仓。末日尝试清算后的残仓估值 **{selected_row['terminal_position_value']:,.2f}元**。",
             "- 原始包无真实历史ST数据，已知覆盖0。价格风险代理无法证明真实非ST；同样无法从日线证明开盘撮合。因此本研究不能宣称已完全满足作业的非ST和真实成交条件。", "",
             "## 全部因子与选股范围", "",
             "先冻结10个因子、3个股票池、50/100只持仓、周/月调仓，共120个候选。每个候选先跑2024，按固定规则选型；随后完整报告120个2025结果，没有只保留盈利策略。", "",
             "| 因子 | 方向或公式 |", "|---|---|"]
    for f, spec in FACTOR_SPECS.items():
        lines.append(f"| {spec[0]} | `{spec[1]}` |")
    lines += ["", "三个范围分别为全部A股、仅主板、过去20日均额前1000；均要求历史有效观察≥120、股价≥2元、过去20日均额≥2000万元、至少18日有成交，额外使用因果价格风险代理。", "",
              "## 每个因子的2024最佳参数在2025的结果", "",
              "| 因子 | 股票池 | 持股/调仓 | 2025收益 | 最大回撤 | 夏普 | 平均仓位 | 末日残仓(元) |", "|---|---|---|---:|---:|---:|---:|---:|"]
    reps = study["selection"]["factor_representatives"]
    for f, eid in reps.items():
        r = next((r for r in evaluation if r["experiment_id"] == eid), None)
        if r:
            lines.append(f"| {r['name']} | {r['universe_name']} | {r['top_n']}/{r['rebalance']} | {r['total_return']:.4%} | {r['max_drawdown']:.4%} | {r['sharpe']:.4f} | {r['mean_invested']:.2%} | {r['terminal_position_value']:,.2f} |")
    lines += ["", "## 2024选型冠军的费用、容量与作业延伸", "",
              "| 区间 | 情景 | 总收益 | 最大回撤 | 夏普 | 平均仓位 | 末日残仓(元) |", "|---|---|---:|---:|---:|---:|---:|"]
    for r in study["experiments"]:
        if r["experiment_id"] == selected and r["period_role"] != "selection_2024":
            lines.append(f"| {r['start']}–{r['end']} | {r['scenario']} | {r['total_return']:.4%} | {r['max_drawdown']:.4%} | {r['sharpe']:.4f} | {r['mean_invested']:.2%} | {r['terminal_position_value']:,.2f} |")
    if study.get("benchmark_comparison"):
        b=study["benchmark_comparison"]
        lines += ["",f"相同股票池、持股数和调仓频率的高成交额对照2025总收益{b['benchmark_total_return']:.4%}；所选策略减对照的总收益差{b['total_return_difference']:.4%}。这是同一执行模型的策略对照，不是官方指数alpha。配对10日循环区块重采样的收益差95%区间[{b['difference_ci95_low']:.4%}, {b['difference_ci95_high']:.4%}]，依赖平稳假设且不修正选型偏差。"]
    validation=study.get("validation",{})
    lines += ["","## 数值验收状态","",f"独立验收状态：**{validation.get('status','not_run')}**；通过{validation.get('passed',0)}/{validation.get('total',0)}项。没有真实ST和竞价深度，验收通过仍不能确认实际ST剔除或开盘成交。"]
    if validation.get("precision_summary"):
        lines.append("独立重建的最大金额差（元）：`"+json.dumps(validation["precision_summary"],ensure_ascii=False)+"`；绝对容差0.001元（0.1分），覆盖范围见validation.json。")
    lines += ["", "基准费用：单边佣金1bp、最低5元、卖出印花税5bp、双边过户费0.1bp；目标滑点5bp后按不利方向舍入至0.01元，实际滑点可大于5bp。gross将费用和滑点置0；stress手续费乘2且目标滑点10bp后再按价格刻度舍入；fees_only关闭滑点，保留其余费用；capacity_tight把前均额和当日额参与率从1%降到0.1%。敏感性及作业延伸只用于2024入围项，完整名单见selection.shortlist；作业延伸2025-01-02至2026-09-30沿用2024选型和参数。", "",
              "## 会计、时序与可执行性", "",
              "金额和收益使用float64，CSV以17位有效数字保存；展示四位百分数不代表现实成交精度。完整逐日净值、末日持仓、订单与账本及计算代码可复核。核对结果见validation.json；手续费、现金、持仓和净值的数值误差与容差独立报告。", "",
              "规则以t日收盘冻结目标，最早t+1开盘成交；先卖后买、禁止当日新买份额卖出、无融券杠杆、整手和科创最低股数限制、无现金透支、无失败低排名替补。公司行为日拒单、缺价减记、末日未成交残仓均留痕。", "",
              "## 数据与统计边界", ""]
    lines += [f"{i+1}. {s}" for i, s in enumerate(study["limitations"])]
    lines += ["", "原始P如输出均未经多重校正。候选多、窗口重叠且同源回顾性选型，不能把单项原始P解释为独立确认。未计算或未完成的检查不记作通过。", "",
              "## 文件", "", "- experiments.csv：所有候选/区间/情景，不省略失败，失败另见failures.json。", "- curves.csv、splits.csv：代表策略逐日路径与半年分段。", "- runs/：逐候选日净值及详细入围订单、成交、末日持仓。", "- protocol.json：执行前冻结的搜索空间、费用、选型与输入说明。", "- result.json、validation.json：结构化结果与真实验证状态。", "- strategies/：每个因子的示意积木模板（固定100只/周调仓）；所选策略精确复现请成对使用selected_strategy.json和selected_config.json。", "", f"失败项数量：{len(study['failures'])}。"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_research(progress=print, quick=False):
    resource_policy = reserve_performance_cores()
    study_id = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("study_%Y%m%d_%H%M%S")
    out = RESEARCH_ROOT / study_id
    out.mkdir(parents=True)
    grid = candidate_grid()
    if quick:
        grid = [r for r in grid if r["factor"] in ("dr5_reversal", "lowvol20") and r["universe"] == "liquid1000" and r["top_n"] == 100 and r["rebalance"] == "weekly"]
    protocol = {"frozen_at": now(), "input": "feathers.zip only; original unchanged", "grid": grid,
                "factors": {f: {"name": s[0], "formula": s[1]} for f, s in FACTOR_SPECS.items()},
                "periods": PERIODS, "base_config": asdict(base_config()), "resource_policy": resource_policy,
                "selection_rule": "2024 only, mean_invested>=.25,max_drawdown>=-.40,writeoff_loss=0; max sharpe,then return,then ID; explicit fallback",
                "retrospective": True, "true_ST_coverage": 0, "quick_development_run": quick}
    save_json(out / "protocol.json", protocol)
    progress(f"{study_id} 读取原始矩阵，候选{len(grid)}；CPU保留{resource_policy['reserved_logical_processors']}")
    m = Market()
    matrices = factor_matrices(m)
    eligible_cache, score_cache = {}, {}
    experiments, failures, curves, splits, execution = [], [], [], [], []
    study = {"study_id": study_id, "generated_at": now(), "status": "running", "protocol": protocol,
             "experiments": experiments, "curves": curves, "splits": splits, "execution": execution,
             "st_coverage": [], "limitations": LIMITATIONS, "failures": failures, "selection": {},
             "uncertainty": [], "ic": [],
             "validation": {"status": "not_run"}, "files": {"report": f"{study_id}/report.md", "experiments": f"{study_id}/experiments.csv", "result": f"{study_id}/result.json", "protocol": f"{study_id}/protocol.json"}}

    def get_score(candidate, cfg):
        key = candidate["universe"]
        if key not in eligible_cache:
            eligible_cache[key] = m.eligible(cfg)
        score_key = (candidate["factor"], key)
        if score_key not in score_cache:
            # Bound the cache: each score is about 72MB on the full archive.
            if len(score_cache) >= 5:
                score_cache.pop(next(iter(score_cache)))
            score_cache[score_key] = score_for(candidate, eligible_cache[key], matrices)
        return score_cache[score_key]

    def run_one(candidate, role, scenario="base", multiplier=1., detailed=False, **extra):
        cfg = candidate_config(candidate, role, **extra)
        tick = time.monotonic()
        try:
            score = get_score(candidate, cfg)
            result = simulate(m, score, cfg, cost_multiplier=multiplier, keep_trades=detailed)
            persist_simulation(out, candidate["experiment_id"], role, scenario, result, detailed)
            row = experiment_row(candidate, role, scenario, cfg, result)
            row["elapsed_seconds"] = time.monotonic() - tick
            experiments.append(row)
            if detailed:
                d = result["daily"]
                nav = d.nav.to_numpy()
                peaks = np.maximum.accumulate(np.r_[cfg.capital, nav])[1:]
                for record, index, drawdown in zip(rows(d[["date", "invested_fraction"]]), nav / cfg.capital, nav / peaks - 1):
                    curves.append({"experiment_id": candidate["experiment_id"], "period_role": role, "scenario": scenario, **record, "nav_index": float(index), "drawdown": float(drawdown)})
                splits.extend({"experiment_id": candidate["experiment_id"], "period_role": role, "scenario": scenario, **r} for r in split_returns(d, cfg.capital))
                execution.extend({"experiment_id": candidate["experiment_id"], "period_role": role, "scenario": scenario,
                                  "reason": reason, "count": int(count), "notional": None} for reason, count in result["blocked"].items())
                if role == "evaluation_2025" and scenario == "base":
                    study["uncertainty"].append({"experiment_id":candidate["experiment_id"],**descriptive_uncertainty(d)})
            return row, result
        except Exception as exc:
            failure = {"experiment_id": candidate["experiment_id"], "period_role": role, "scenario": scenario, "error": str(exc), "traceback": traceback.format_exc()}
            failures.append(failure)
            save_json(out / "failures.json", failures)
            progress("失败后继续队列 " + json.dumps({k: failure[k] for k in ("experiment_id", "period_role", "scenario", "error")}, ensure_ascii=False))
            return None, None

    started = time.monotonic()
    for number, candidate in enumerate(grid, 1):
        run_one(candidate, "selection_2024")
        if number <= 3 or number % 10 == 0:
            elapsed = time.monotonic() - started
            progress(f"2024选型 {number}/{len(grid)}；已用{elapsed:.1f}s；阶段预计剩余{elapsed/number*(len(grid)-number):.1f}s")
    study["selection"] = choose_on_2024(experiments)
    save_json(out / "selection_frozen_before_2025.json", {"frozen_at": now(), **study["selection"]})
    selected_ids = set(study["selection"]["shortlist"]) | set(study["selection"]["factor_representatives"].values())
    for number, candidate in enumerate(grid, 1):
        run_one(candidate, "evaluation_2025", detailed=candidate["experiment_id"] in selected_ids)
        if number <= 3 or number % 10 == 0:
            progress(f"2025评价 {number}/{len(grid)}；累计{time.monotonic()-started:.1f}s")
    shortlist = [r for r in grid if r["experiment_id"] in study["selection"]["shortlist"]]
    for number, candidate in enumerate(shortlist, 1):
        progress(f"敏感性和作业延伸 {number}/{len(shortlist)} {candidate['experiment_id']}")
        for scenario, multiplier, extra in (("gross", 0., {}), ("stress", 2., {}), ("fees_only", 1., {"slippage": 0.}),
                                             ("capacity_tight", 1., {"max_participation": .001, "max_daily_participation": .001})):
            run_one(candidate, "evaluation_2025", scenario, multiplier, detailed=True, **extra)
        run_one(candidate, "assignment_2025_2026sep", detailed=True)
        cfg=candidate_config(candidate,"evaluation_2025")
        try:
            ic_daily, ic_summary = ic_analysis(m,get_score(candidate,cfg),eligible_cache[candidate["universe"]],cfg)
            destination=out/"runs"/candidate["experiment_id"]
            ic_daily.to_csv(destination/"ic_daily_2025.csv",index=False,encoding="utf-8-sig",float_format="%.17g")
            study["ic"].append({"experiment_id":candidate["experiment_id"],**finite_json(ic_summary)})
        except Exception as exc:
            failures.append({"experiment_id":candidate["experiment_id"],"period_role":"evaluation_2025","scenario":"ic","error":str(exc)})
    for f, spec in FACTOR_SPECS.items():
        save_json(out / "strategies" / (f + ".json"), {"format": "daily-factor-strategy", "version": 1,
                  "name": spec[0], "score": spec[2], "filter": node("is_finite", node("close")),
                  "selection": {"top_n": 100, "rebalance": "weekly", "direction": "desc"}, "packs": []})
    selected_candidate=next(c for c in grid if c["experiment_id"]==study["selection"]["selected_id"])
    save_json(out/"selected_config.json",asdict(candidate_config(selected_candidate,"evaluation_2025")))
    selected_spec=FACTOR_SPECS[selected_candidate["factor"]]
    save_json(out/"selected_strategy.json",{"format":"daily-factor-strategy","version":1,"name":selected_candidate["name"],
              "score":selected_spec[2],"filter":node("is_finite",node("close")),
              "selection":{"top_n":selected_candidate["top_n"],"rebalance":selected_candidate["rebalance"],"direction":"desc"},"packs":[]})
    study["files"].update(selected_strategy=f"{study_id}/selected_strategy.json",selected_config=f"{study_id}/selected_config.json")
    from execution_metadata import price_risk_proxy
    risk_proxy=price_risk_proxy(m,base_config().risk_proxy_window)
    for role, (start, end) in PERIODS.items():
        idx = np.flatnonzero((m.dates >= start) & (m.dates <= end))
        observed = np.isfinite(m.raw["close"][idx])
        study["st_coverage"].append({"scope": role, "total_code_days": int(observed.sum()), "known_st": 0,
             "known_non_st": 0, "unknown": int(observed.sum()), "proxy_excluded_code_days": int((observed & risk_proxy[idx]).sum()),
             "note": "分母为区间有有效原始收盘的code-day；代理剔除见每个实验诊断，不代表真实ST状态。"})
    study.update(generated_at=now(), status="complete" if not failures else "partial")
    save_json(out / "result.json", finite_json(study))
    frame = pd.DataFrame([{k: v for k, v in r.items() if k not in ("config", "terminal", "execution_diagnostics")} for r in experiments])
    frame.to_csv(out / "experiments.csv", index=False, encoding="utf-8-sig", float_format="%.17g")
    for key in ("curves", "splits", "execution", "st_coverage"):
        pd.DataFrame(study[key]).to_csv(out / (key + ".csv"), index=False, encoding="utf-8-sig", float_format="%.17g")
    save_json(out / "failures.json", failures)
    write_report(out, study)
    save_json(RESEARCH_ROOT / "latest.json", {"study_id": study_id, "result": study_id + "/result.json"})
    progress(f"完成{study_id}；共{len(experiments)}情景；失败{len(failures)}；用时{time.monotonic()-started:.1f}s")
    return study, m


def latest_research():
    pointer = RESEARCH_ROOT / "latest.json"
    if not pointer.exists():
        return {"status": "not_run", "experiments": [], "curves": [], "splits": [], "execution": [], "st_coverage": []}
    info = json.loads(pointer.read_text(encoding="utf-8"))
    result=json.loads((RESEARCH_ROOT / info["result"]).read_text(encoding="utf-8"))
    validation=RESEARCH_ROOT/info["study_id"]/"validation.json"
    if validation.exists():
        audit=json.loads(validation.read_text(encoding="utf-8"))
        result["validation"]={**result.get("validation",{}),**{k:audit[k] for k in ("status","passed","total","scope","money_absolute_tolerance") if k in audit}}
    repair_folder=RESEARCH_ROOT/info["study_id"]/"feasibility_repair"
    repair_file=repair_folder/"feasibility_repair.json"
    repair_validation=repair_folder/"feasibility_validation.json"
    if repair_file.exists():
        repair=json.loads(repair_file.read_text(encoding="utf-8"))
        audit=json.loads(repair_validation.read_text(encoding="utf-8")) if repair_validation.exists() else {"status":"not_run"}
        result["feasibility_repair"]={k:repair.get(k) for k in ("repair_id","posthoc","protocol","selected_id","selection_by_liquidation_only","limitations","settlement_disclosure")}
        result["feasibility_repair"]["validation"]={k:audit[k] for k in ("status","passed","total","scope","money_absolute_tolerance") if k in audit}
        if audit.get("status")=="passed" and repair.get("status")=="complete":
            result["baseline_generated_at"]=result["generated_at"]
            result["generated_at"]=repair["generated_at"]
            result["baseline_experiment_count"]=len(result["experiments"])
            for key in ("experiments","curves","splits","execution"):
                result[key]=result.get(key,[])+repair.get(key,[])
            result["presentation_experiment_count"]=len(result["experiments"])
            for key,path in repair.get("files",{}).items():
                result["files"]["repair_"+key]=info["study_id"]+"/"+path
            result["files"].update(repair_protocol=info["study_id"]+"/feasibility_repair/protocol.json",
                                  repair_result=info["study_id"]+"/feasibility_repair/feasibility_repair.json",
                                  repair_validation=info["study_id"]+"/feasibility_repair/feasibility_validation.json")
            result["limitations"]+=repair.get("limitations",[])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="明确标记的开发用小范围运行，不代替正式全候选结果")
    args = parser.parse_args()
    run_research(quick=args.quick)
