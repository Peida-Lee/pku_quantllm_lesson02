"""Run the frozen factor suite, save evidence, and refresh the reviewed dashboard."""
import argparse
import os
import json
import subprocess
import shutil
import sys
import traceback
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from lab import ROOT, SOURCE, Config, FACTOR_INFO, Market, simulate, ic_analysis, reference_curve, performance

OUTPUTS = ROOT / "outputs"
_plugin_root = Path.home() / ".codex" / "plugins" / "cache" / "openai-curated-remote" / "data-analytics"
_plugin_versions = sorted(_plugin_root.glob("*"), key=lambda p: tuple(int(v) for v in p.name.split(".") if v.isdigit())) if _plugin_root.exists() else []
PLUGIN = Path(os.environ.get("FACTOR_DATA_PLUGIN", str(_plugin_versions[-1] if _plugin_versions else _plugin_root)))
NODE = Path(os.environ.get("FACTOR_NODE", shutil.which("node") or "node"))

def json_rows(frame):
    return json.loads(frame.to_json(orient="records", double_precision=14))

def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

def make_snapshot(result, complete=False):
    path = ROOT / "dashboard" / "src" / "data.json"
    if not path.exists(): return
    previous = json.loads(path.read_text(encoding="utf-8"))
    run = result["run_id"]
    source = {
        "label": "用户提供的课程行情：feathers.zip；本地历史模拟结果",
        "files": ["feathers.zip", "lab.py", "config.json", f"outputs/{run}/result.json"],
        "executedAt": result["generated_at"], "timezone": "Asia/Shanghai",
        "coverage": {"start": result["config"]["start"], "end": result["config"]["end"]},
        "filters": ["前一日信号、下一日开盘；动态历史股票池；规则与参数按每次运行保存"],
        "caveats": result["limitations"],
        "evidenceFlow": [{"title": "读取课程原始矩阵", "detail": "源feathers.zip内open/high/low/close/amt/cumadj；原数据未改动"},
                         {"title": "因子与执行", "detail": ("用户积木策略；完整规则见strategy.json和strategy_manifest.json；" if result.get("custom_strategy") else "固定4因子及等权组合；")+"过去窗口构建信号；复权份额、费用、买卖门禁及缺失减记，详见lab.py"},
                         {"title": "当前运行", "detail": json.dumps(result["config"], ensure_ascii=False)}],
    }
    definitions = {
        "summary": [{"label": "年化收益", "definition": "(末日净值/初始资金)^(252/交易日数)-1；2026为不完整年度"},
                    {"label": "夏普", "definition": "日简单收益均值/样本标准差×sqrt(252)，无风险利率取0"},
                    {"label": "最大回撤", "definition": "含初始资金峰值的日收盘净值最大跌幅，保留负号"}],
        "curves": [{"label": "归一化净值", "definition": "nav_index=nav/初始资金，base含费用，gross零费用，stress所有费用与滑点乘2"},
                   {"label": "等权参考", "definition": "前日合格股票池的日收盘复权收益等权平均，无费用；缺收益按0，非可投资指数，未作减记或执行约束"}],
        "yearly": [{"label": "年度收益", "definition": "该年最后净值/该年第一个交易日前的净值-1，2026截至数据末日"}],
        "ic": [{"label": "RankIC", "definition": "信号日因子与次日开盘到第六日开盘的复权5日收益做横截面Spearman相关；完整结果对，HAC滞后5，95%正态近似区间，P仅原始值"}],
    }
    queries = {}
    for key in ["summary", "curves", "yearly", "ic"]:
        queries[key] = {"rows": result[key], "source": {**source, "metricDefinitions": definitions[key]},
                        "methods": [{"language": "python", "code": "python run_experiments.py --config config.json; see lab.py and this run's config.json"}]}
    previous.update({"surface": "dashboard", "title": "日频多因子回测", "generatedAt": result["generated_at"],
                     "run_id": run, "config": result["config"], "files": result["files"], "strategies": result["strategies"], "diagnostics": result["diagnostics"],
                     "custom_strategy": result.get("custom_strategy"), "strategy_manifest": result.get("strategy_manifest"),
                     "status": "reviewed", "buildStatus": "complete" if complete else "updating", "filters": [], "queries": queries})
    from research import latest_research
    research = latest_research()
    if research.get("study_id"):
        research_source = {
            "label":"同一课程feathers.zip的2024选型/2025评价；真实ST状态缺失",
            "files":["feathers.zip","research.py","lab.py","execution_metadata.py",f"research_outputs/{research['study_id']}/result.json"],
            "executedAt":research["generated_at"],"timezone":"Asia/Shanghai",
            "coverage":{"selection":"2024-01-02至2024-12-31","primary":"2025-01-02至2025-12-31","assignment":"2025-01-02至2026-09-30"},
            "caveats":research["limitations"],
            "filters":["仅2024选型；2025全部候选透明报告；价格风险代理不是真实ST剔除"],
            "metricDefinitions":[{"label":"总收益","definition":"末日NAV/10亿元-1，清算未成交残仓按末日收盘估值另列"},
                                 {"label":"平均仓位","definition":"逐日1-现金/NAV均值，反映10亿元容量和拒单导致的闲置"},
                                 {"label":"真实ST未知","definition":"区间有有效原始收盘的code-day全部未知；known-ST和known-non-ST均0"}],
        }
        if research.get("feasibility_repair"):
            research_source["files"].extend([f"research_outputs/{research['study_id']}/feasibility_repair/feasibility_repair.json",
                                               f"research_outputs/{research['study_id']}/feasibility_repair/feasibility_validation.json"])
            research_source["filters"].append("退出补充为发现末日残仓后的后验执行修复；仍固定2024选型，仅按是否清空选择20/40日最短成功窗口")
        for name in ("experiments","curves","splits","execution","st_coverage"):
            queries["research_"+name]={"rows":research.get(name,[]),"source":research_source,
                 "methods":[{"language":"python","code":"python research.py; see study protocol.json, selection_frozen_before_2025.json and validation.json"}]}
        previous["research"]=research
    from research_v4 import attach_snapshot
    attach_snapshot(previous)
    from v4_snapshot import attach_diagnostics
    attach_diagnostics(previous)
    save_json(path, previous)

def build_dashboard():
    if not (ROOT / "dashboard" / "src" / "content" / "dashboard" / "DashboardContent.jsx").exists(): return None
    if not NODE.exists() or not PLUGIN.exists(): return "Data plugin build runtime unavailable; existing backend results remain valid"
    from bootstrap_dashboard import ensure_runtime
    try:
        target = ensure_runtime(ROOT, PLUGIN, NODE)
    except Exception as e:
        return "Dashboard runtime preparation failed: " + str(e)
    result = subprocess.run([str(NODE), str(PLUGIN / "scripts" / "data-app.mjs"), "build", "--project-dir", str(target), "--separate-data"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    (ROOT / "dashboard_build.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode: return "Dashboard build failed: see dashboard_build.log"
    if target != ROOT / "dashboard": shutil.copytree(target / "dist", ROOT / "dashboard" / "dist", dirs_exist_ok=True)
    return None

LIMITATIONS = [
    "历史模拟，固定四因子方向及等权组合；没有调参择优，不构成独立外部验证或实盘收益承诺。",
    "cumadj仅有累计因子：以复权份额合成记账，隐含公司行为收益再投资；无法还原实际现金分红、送转股、税与股份到账。下单折算原股数并执行100股整手、科创至少200股的保守近似；非券商逐股交收账本。",
    "前一日收盘因子、前一日流动性决定股票池与信号；下一日开盘尝试成交。开盘相对因子修正前收盘涨幅≥4.8%不买、跌幅≤−4.8%不卖，较常见涨跌停规则更保守，避免未知ST状态下乐观成交；缺少官方涨跌停价及队列，仍不能证明其他开盘单实际成交。",
    "当日最终amt仅用于拒绝全天无成交日的成交，是事后日线执行审计代理；不用于信号、排名、候选替补或参与率。它不能证明开盘时可成交。",
    "目标份额按当日开盘估值重标后按开盘价近似成交，隐含开盘价可获知并成交的理想化假设；实际须进一步模拟竞价下单时点、可用现金、撮合延迟与滑点。统一4.8%门禁也未精确建模官方限价的0.01元舍入或特殊无涨跌幅日，不能保证识别每个ST限价。",
    "每笔交易上限为前20日均额的1%，固定滑点与费用敏感性不等于真实冲击模型；只约束信号已知流动性，没有逐笔或开盘竞价深度。",
    "T+1通过禁止卖出当日新买份额实现；无融券、杠杆、融资、同日回转；未成交目标保留重试，失败不替补到低排名股票，剩余现金闲置。",
    "未使用末日存活名单筛股；保留历史所有代码。仍缺官方时点股票池、ST及退市事件。持仓缺收盘时先按当日有效开盘、再按旧价估值，连续缺20日减记为0，没有伪造末日前清仓；这是一项保守情景，并非实际退市清偿金额。",
    "最后一日两只股票缺因子，相关因子/价格不填补。成交笔数末日缺失，本轮四因子不依赖该字段。",
    "动态股票池等权参考是无费用的描述性收盘收益序列，缺收益按0，未模拟减记/成交门禁；不是沪深300或可交易基准，不能把相对它的差额称为严格可交易alpha。",
    "RankIC使用有完整未来开盘结果的股票对，存在结果缺失选择；输出配对覆盖率。5日收益窗口重叠，用HAC滞后5给近似区间和原始P，多因子探索不视作确认性检验。",
    "2021–2023与2024至末日分别列出时间段。V1是预先指定分段；自定义积木反复试验不能视为独立留出验证。所有历史为同一数据源，2026年度尚未结束。",
]

def run_suite(cfg, market=None, progress=print, dashboard_complete=False, build=True, custom_strategy=None):
    strategy_document = None
    strategy_manifest = None
    factor_definitions = FACTOR_INFO
    if custom_strategy is not None:
        from strategy_blocks import validate_strategy
        validation = validate_strategy(custom_strategy)
        strategy_document = validation["strategy"]
        selection = strategy_document.get("selection", {})
        overrides = {k: selection[k] for k in ["top_n", "rebalance"] if k in selection}
        cfg = replace(cfg, **overrides)
    cfg.validate()
    if cfg.execution_model == "daily_conservative":
        from research import LIMITATIONS as active_limitations
    else:
        active_limitations = LIMITATIONS
    OUTPUTS.mkdir(exist_ok=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    run_id = now.strftime("run_%Y%m%d_%H%M%S_%f")
    out = OUTPUTS / run_id
    out.mkdir()
    save_json(out / "config.json", asdict(cfg))
    if strategy_document is not None: save_json(out / "strategy.json", strategy_document)
    save_json(out / "frozen_protocol.json", {"created_at": now.isoformat(), "factors": factor_definitions if strategy_document is None else {"custom": strategy_document},
        "scenarios": {"base": 1, "gross": 0, "stress": 2}, "selection": "Report all scenarios; custom strategies are user-authored exploratory experiments", "config": asdict(cfg)})
    progress("读取数据与计算过去窗口因子")
    m = market or Market()
    if cfg.start < m.dates[0].strftime("%Y-%m-%d") or cfg.end > m.dates[-1].strftime("%Y-%m-%d"):
        raise ValueError("请求区间超出数据日期覆盖")
    if strategy_document is None:
        eligible, scores = m.scores(cfg)
    else:
        from strategy_blocks import compile_strategy
        progress("校验并编译策略积木")
        compiled = compile_strategy(m, cfg, strategy_document)
        eligible, scores = compiled["eligible"], {"custom": compiled["score"]}
        strategy_manifest = compiled["meta"]
        save_json(out / "strategy_manifest.json", strategy_manifest)
        factor_definitions = {"custom": {"name": strategy_document["name"],
            "formula": strategy_manifest.get("formula", json.dumps(strategy_document["score"], ensure_ascii=False)),
            "hypothesis": "用户用积木组合定义的探索性规则；同样遵守信号滞后与交易可行性近似"}}
    summary, curve_frames, yearly, ic_records, failures = [], [], [], [], []
    diagnostics, strategies, ic_frames = {}, {}, []
    scenarios = [("base",1.), ("gross",0.), ("stress",2.)]
    for factor, info in factor_definitions.items():
        for scenario, multiplier in scenarios:
            progress(f"回测 {info['name']} / {scenario}")
            try:
                r = simulate(m, scores[factor], cfg, multiplier, keep_trades=scenario=="base")
                destination = out / scenario / factor
                destination.mkdir(parents=True)
                d = r["daily"]
                d.to_csv(destination / "daily.csv", index=False, encoding="utf-8-sig",float_format="%.17g")
                if scenario == "base":
                    r["trades"].to_csv(destination / "trades.csv", index=False, encoding="utf-8-sig",float_format="%.17g")
                    r["holdings"].to_csv(destination / "holdings.csv", index=False, encoding="utf-8-sig",float_format="%.17g")
                    strategies[factor] = {key: f"{run_id}/base/{factor}/{key}.csv" for key in ["daily", "trades", "holdings"]}
                    diagnostics[factor] = r["blocked"]
                    save_json(destination / "blocked_attempts.json", r["blocked"])
                    if cfg.execution_model == "daily_conservative":
                        r["orders"].to_csv(destination/"orders.csv",index=False,encoding="utf-8-sig",float_format="%.17g")
                        save_json(destination/"execution_diagnostics.json",r["execution_diagnostics"])
                        save_json(destination/"terminal.json",r["terminal"])
                        strategies[factor].update({"orders":f"{run_id}/base/{factor}/orders.csv",
                                                  "execution_diagnostics":f"{run_id}/base/{factor}/execution_diagnostics.json",
                                                  "terminal":f"{run_id}/base/{factor}/terminal.json"})
                summary.append({"factor": factor, "name": info["name"], "scenario": scenario, **r["metrics"]})
                cf = d[["date", "nav"]].copy()
                cf["factor"], cf["name"], cf["scenario"] = factor, info["name"], scenario
                cf["nav_index"] = cf.nav / cfg.capital
                cf["drawdown"] = cf.nav / np.maximum.accumulate(np.r_[cfg.capital, cf.nav.to_numpy()])[1:] - 1
                curve_frames.append(cf)
                for year, part in d.groupby(d.date.str[:4], sort=True):
                    start_pos = d.index.get_loc(part.index[0])
                    start_nav = cfg.capital if start_pos == 0 else float(d.nav.iloc[start_pos-1])
                    yearly.append({"factor": factor, "name": info["name"], "scenario": scenario, "year": str(year), "return": float(part.nav.iloc[-1]/start_nav-1), "days": len(part)})
                for label, mask in [("2021–2023", d.date < "2024-01-01"), ("2024–末日", d.date >= "2024-01-01")]:
                    part = d[mask]
                    if not part.empty:
                        start_pos = d.index.get_loc(part.index[0])
                        start_nav = cfg.capital if start_pos == 0 else float(d.nav.iloc[start_pos-1])
                        yearly.append({"factor": factor, "name": info["name"], "scenario": scenario, "year": label, "return": float(part.nav.iloc[-1]/start_nav-1), "days": len(part)})
            except Exception as e:
                failures.append({"factor": factor, "scenario": scenario, "error": str(e)})
                (out / f"failure_{factor}_{scenario}.txt").write_text(traceback.format_exc(), encoding="utf-8")
                progress(f"该项失败，继续其余队列：{factor}/{scenario}: {e}")
        try:
            ic_daily, ic = ic_analysis(m, scores[factor], eligible, cfg)
            if ic:
                ic_records.append({"factor": factor, "name": info["name"], **ic})
                ic_daily["factor"], ic_daily["name"] = factor, info["name"]
                ic_frames.append(ic_daily)
        except Exception as e:
            failures.append({"factor": factor, "scenario": "ic", "error": str(e)})
    indices = np.flatnonzero((m.dates >= cfg.start) & (m.dates <= cfg.end) & (np.arange(len(m.dates)) > 0))
    reference = reference_curve(m, eligible, cfg, indices)
    reference.to_csv(out / "reference.csv", index=False, encoding="utf-8-sig")
    reference["factor"], reference["name"], reference["scenario"] = "reference", "股票池等权参考（无费用）", "base"
    reference["nav_index"] = reference.nav / cfg.capital
    reference["drawdown"] = reference.nav / np.maximum.accumulate(np.r_[cfg.capital, reference.nav.to_numpy()])[1:]-1
    curve_frames.append(reference[["date","nav","factor","name","scenario","nav_index","drawdown"]])
    curves = pd.concat(curve_frames, ignore_index=True)
    curves.to_csv(out / "curves.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(summary).to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(yearly).to_csv(out / "yearly.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(ic_records).to_csv(out / "ic_summary.csv", index=False, encoding="utf-8-sig")
    if ic_frames: pd.concat(ic_frames, ignore_index=True).to_csv(out / "ic_daily.csv", index=False, encoding="utf-8-sig")
    files = {key: f"{run_id}/{filename}" for key,filename in {"report":"report.md", "summary":"summary.csv", "curves":"curves.csv", "yearly":"yearly.csv", "ic":"ic_summary.csv", "config":"config.json"}.items()}
    if strategy_document is not None:
        files.update({"strategy":f"{run_id}/strategy.json", "strategy_manifest":f"{run_id}/strategy_manifest.json"})
    result = {"run_id": run_id, "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "config": asdict(cfg),
        "summary": summary, "curves": json_rows(curves), "yearly": yearly, "ic": ic_records, "diagnostics": diagnostics,
        "strategies": strategies, "files": files, "failures": failures, "status": "complete" if not failures else "partial",
        "factor_definitions": factor_definitions, "limitations": active_limitations,
        "custom_strategy": strategy_document, "strategy_manifest": strategy_manifest,
        "source": {"file":"feathers.zip", "rows":len(m.dates), "symbols":len(m.symbols), "date_start":str(m.dates[0].date()), "date_end":str(m.dates[-1].date())}}
    report_lines = ["# 日频多因子回测初步实验", "", f"运行：`{run_id}`；生成时间：{result['generated_at']}（Asia/Shanghai）。", "",
        f"采用用户提供的课程日频数据，{len(m.dates):,}个日期、{len(m.symbols):,}个历史代码。初始资金{cfg.capital:,.0f}元，目标持有排名前{cfg.top_n}只，{cfg.rebalance}调仓，仅多头。", "",
        "## 实验与执行协议", "", "本次规则与参数在执行前保存于frozen_protocol.json；积木策略另保存完整strategy.json和编译清单。全部报告费用情景。交互式试验可能包含人为反复调参，不能当作未看结果的确认性验证。", ""]
    for factor,info in factor_definitions.items(): report_lines.append(f"- **{info['name']}**：`{info['formula']}`；{info['hypothesis']}。")
    report_lines += ["", f"信号在t日收盘形成，t+1日开盘尝试；排名并列按代码排序。股票池要求：至少{cfg.min_history}个有效历史价格观察、价格不低于{cfg.min_price:g}元、前20日平均成交金额至少{cfg.min_avg_amount:g}元且至少{cfg.min_active_days20}日有成交；股票范围{cfg.universe}、流动性TopN={cfg.liquidity_top_n}（0不限），ST政策={cfg.st_policy}，执行模型={cfg.execution_model}，末日尝试清仓={cfg.liquidate_end}。真实ST身份缺失，价格代理不能视为真实非ST。", "",
        f"费用假设：双边佣金{cfg.commission*10000:g}bp、单笔最低{cfg.min_commission:g}元，目标双边滑点{cfg.slippage*10000:g}bp，卖出印花税2023-08-28前10bp、之后5bp，双边过户费2022-04-29前0.2bp、之后0.1bp。daily_conservative使用不利0.01元价格刻度舍入后的真实本模型成交额计算税费和容量；notional为参考开盘额、execution_notional为成交额、slippage为价差成本，现金仍以参考额加减总成本对账。gross费用和目标滑点置零，stress费率乘2且目标滑点乘2后再按刻度舍入，实际滑点不保证线性翻倍。均为研究假设，不代表券商撮合或合同。", "",
        "## 主结果（base，含费用）", "", "| 因子 | 总收益 | 年化收益 | 夏普 | 最大回撤 | 平均仓位 |", "|---|---:|---:|---:|---:|---:|"]
    for r in summary:
        if r["scenario"] == "base": report_lines.append(f"| {r['name']} | {r['total_return']:.2%} | {r['annual_return']:.2%} | {r['sharpe']:.3f} | {r['max_drawdown']:.2%} | {r['mean_invested']:.2%} |")
    report_lines += ["", "## 费用敏感性（年化收益）", "", "| 因子 | 零费用 | 基准费用 | 双倍费用/滑点 |", "|---|---:|---:|---:|"]
    for f,info in factor_definitions.items():
        lookup = {r['scenario']:r['annual_return'] for r in summary if r['factor']==f}
        report_lines.append(f"| {info['name']} | " + " | ".join(f"{lookup[k]:.2%}" if k in lookup else "失败" for k in ["gross","base","stress"]) + " |")
    report_lines += ["", "## 因子5日RankIC", "", "区间为HAC(5)正态近似95%区间，P为未经多重校正的原始双侧P。正相关是预设方向的预测性描述，不能替代含费用可行组合收益。", "", "| 因子 | 平均RankIC | 95%区间 | 原始P | 日数 | 配对覆盖率 |", "|---|---:|---|---:|---:|---:|"]
    for r in ic_records:
        pv = f"{r['p_raw_hac_normal']:.4g}" if r['p_raw_hac_normal'] is not None else "缺失"
        report_lines.append(f"| {r['name']} | {r['mean_rank_ic']:.4f} | [{r['ci95_low']:.4f}, {r['ci95_high']:.4f}] | {pv} | {r['days']} | {r['mean_pair_coverage']:.2%} |")
    report_lines += ["", "## 时间分段观察（base）", "", "2024年起为沿用V1的观察分段。自定义积木可反复试验，这些时间段不能自动当作严格留出样本；仍是同一数据源的历史观察。", "", "| 因子 | 2021–2023区间收益 | 2024–末日区间收益 |", "|---|---:|---:|"]
    for f,info in factor_definitions.items():
        lookup = {r['year']:r['return'] for r in yearly if r['factor']==f and r['scenario']=='base'}
        report_lines.append(f"| {info['name']} | " + " | ".join(f"{lookup[k]:.2%}" if k in lookup else "无覆盖" for k in ["2021–2023","2024–末日"]) + " |")
    report_lines += ["", "## 可行性门禁与局限", ""] + [f"{i+1}. {text}" for i,text in enumerate(active_limitations)]
    report_lines += ["", "## 审计与文件", "", "summary.csv为全部策略/费用情景；curves.csv为完整日净值；yearly.csv为年度及时间段；ic_summary.csv/ic_daily.csv为IC结果；base/<factor>/为逐日资金、交易账本、末日持仓和拒单计数。WRITE_OFF为估值损失，不能解释为成交。", "",
        "输入原压缩包不作修改。接口只提供本地历史回测，没有券商、行情联网或真实订单接口。复现：`python run_experiments.py --config config.json`。", "",
        "本轮失败项：" + (json.dumps(failures,ensure_ascii=False) if failures else f"无（{len(factor_definitions)*3}个策略/费用情景和{len(ic_records)}组IC完成）")]
    (out / "report.md").write_text("\n".join(report_lines)+"\n", encoding="utf-8")
    save_json(out / "result.json", result)
    save_json(OUTPUTS / "latest.json", {"run_id":run_id, "result":f"{run_id}/result.json"})
    make_snapshot(result, complete=dashboard_complete)
    if build:
        progress("构建仪表盘")
        build_error = build_dashboard()
        result["dashboard_build_error"] = build_error
        save_json(out / "result.json", result)
        if build_error: progress(build_error)
    progress("完成 " + run_id)
    return result, m

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--strategy", help="可选：策略积木JSON文件；不指定则运行V1五个固定因子")
    args = parser.parse_args()
    cfg = Config.from_dict(json.loads(Path(args.config).read_text(encoding="utf-8")))
    doc = json.loads(Path(args.strategy).read_text(encoding="utf-8")) if args.strategy else None
    run_suite(cfg, progress=lambda s:print(s,flush=True), dashboard_complete=args.complete, build=not args.no_build, custom_strategy=doc)
