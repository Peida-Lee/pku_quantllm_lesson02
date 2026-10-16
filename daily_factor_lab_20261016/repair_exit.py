"""Post-hoc exit feasibility supplement; retain the original 2024 selection.

The diagnosis is observed end-date residuals, never a reason to replace the
selected factor using 2025 returns. Both 20/40-day schedules are reported.
"""
import argparse
import json
import time
from dataclasses import replace, asdict
from pathlib import Path

import numpy as np
import pandas as pd

from research import (ROOT, Market, Config, candidate_grid, candidate_config, factor_matrices,
                      score_for, persist_simulation, experiment_row, split_returns,
                      save_json, finite_json, now, reserve_performance_cores, simulate)


def run_repair(study_path, output=None, progress=print):
    reserve_performance_cores()
    study_path=Path(study_path)
    baseline=json.loads((study_path/"result.json").read_text(encoding="utf-8"))
    out=Path(output) if output else study_path/"feasibility_repair"
    out.mkdir(parents=True,exist_ok=False)
    shortlist=baseline["selection"]["shortlist"]
    candidates=[r for r in candidate_grid() if r["experiment_id"] in shortlist]
    protocol={"frozen_at":now(),"posthoc":True,"diagnosis":"原始单日退出留下大额残仓，不能声称已清空兑现。",
              "factor_selection_unchanged":True,"selected_id":baseline["selection"]["selected_id"],
              "original_shortlist":shortlist,"wind_down_windows":[20,40],
              "normal_roles":["evaluation_2025","assignment_2025_2026sep"],
              "evaluation_2025_extra_scenarios":["wind_down20_stress","wind_down20_capacity_tight","wind_down40_capacity_tight"],
              "selection_rule":"每个原入围候选/区间先20日，40日其次；只按残仓≤0.001元且0持仓选最短成功窗口，不按收益，未成功则不选。",
              "calendar_assumption":"按源交易日历预排区间最后20/40个交易日期；没有使用未来价格或成交额选择开始日。",
              "baseline_study":baseline["study_id"],"expected_scenarios":len(shortlist)*7}
    save_json(out/"protocol.json",protocol)
    repair={"repair_id":out.name,"study_id":baseline["study_id"],"generated_at":now(),"status":"running","posthoc":True,
            "protocol":protocol,"original_shortlist":shortlist,"selected_id":baseline["selection"]["selected_id"],
            "experiments":[],"curves":[],"splits":[],"execution":[],"selection_by_liquidation_only":[],
            "failures":[],"validation":{"status":"not_run"},"files":{},
            "limitations":["后验退出规则修复，2024因子与股票池选型不变；不当作新的独立验证。",
                           "只核实声明日线模型的退出，真实ST、竞价撮合与实物公司行为仍不可核实。",
                           "分批退出开始日在日历上预排，不根据未来价格/成交额择时；真实未来停牌或流动性变化仍可能阻止清仓。"]}
    progress("读取同一原始zip，开始退出修复补充，共"+str(protocol["expected_scenarios"])+"情景")
    m=Market()
    matrices=factor_matrices(m)
    cache={}
    count=0
    for candidate in candidates:
        jobs=[(role,"wind_down"+str(window),window,1.,{}) for role in protocol["normal_roles"] for window in (20,40)]
        jobs += [("evaluation_2025","wind_down20_stress",20,2.,{}),
                 ("evaluation_2025","wind_down20_capacity_tight",20,1.,{"max_participation":.001,"max_daily_participation":.001}),
                 ("evaluation_2025","wind_down40_capacity_tight",40,1.,{"max_participation":.001,"max_daily_participation":.001})]
        for role,scenario,window,multiplier,extra in jobs:
            count+=1
            progress(f"退出补充{count}/{protocol['expected_scenarios']} {candidate['experiment_id']} {role} {scenario}")
            try:
                cfg=replace(candidate_config(candidate,role),wind_down_days=window,**extra).validate()
                key=candidate["experiment_id"]
                if key not in cache:
                    cache[key]=score_for(candidate,m.eligible(cfg),matrices)
                result=simulate(m,cache[key],cfg,cost_multiplier=multiplier,keep_trades=True)
                folder=persist_simulation(out,key,role,scenario,result,detailed=True)
                row=experiment_row(candidate,role,scenario,cfg,result)
                original=next(r for r in baseline["experiments"] if r["experiment_id"]==key and r["period_role"]==role and r["scenario"]=="base")
                row.update(wind_down_days=window,cost_multiplier=multiplier,posthoc_execution_repair=True,
                           folder=folder.relative_to(out).as_posix(),baseline_total_return=original["total_return"],
                           baseline_residual_value=original["terminal_position_value"])
                repair["experiments"].append(row)
                daily=result["daily"]
                nav=daily.nav.to_numpy()
                peaks=np.maximum.accumulate(np.r_[cfg.capital,nav])[1:]
                repair["curves"].extend({"experiment_id":key,"period_role":role,"scenario":scenario,"date":r.date,
                     "nav_index":float(r.nav/cfg.capital),"drawdown":float(dd),"invested_fraction":float(r.invested_fraction)}
                     for r,dd in zip(daily.itertuples(),nav/peaks-1))
                repair["splits"].extend({"experiment_id":key,"period_role":role,"scenario":scenario,**s} for s in split_returns(daily,cfg.capital))
                repair["execution"].extend({"experiment_id":key,"period_role":role,"scenario":scenario,"reason":reason,"count":int(number),"notional":None}
                         for reason,number in result["blocked"].items())
            except Exception as exc:
                repair["failures"].append({"experiment_id":candidate["experiment_id"],"period_role":role,"scenario":scenario,"error":str(exc)})
                progress("失败后继续："+str(exc))
    for candidate in candidates:
        for role in protocol["normal_roles"]:
            choice=None
            for window in (20,40):
                row=next((r for r in repair["experiments"] if r["experiment_id"]==candidate["experiment_id"] and r["period_role"]==role and r["scenario"]=="wind_down"+str(window)),None)
                if row and row["terminal_positions"]==0 and row["terminal_position_value"]<=.001:
                    choice=row
                    break
            repair["selection_by_liquidation_only"].append({"experiment_id":candidate["experiment_id"],"period_role":role,
                   "scenario":choice["scenario"] if choice else None,"wind_down_days":choice["wind_down_days"] if choice else None,
                   "fully_liquidated":bool(choice),"reason":"20先、40后；只按零残仓与零持仓，不按收益" if choice else "两个预定窗口都未清空，不推荐为已退出结果"})
    repair.update(generated_at=now(),status="complete" if not repair["failures"] else "partial")
    for key in ("experiments","curves","splits","execution"):
        values=repair[key]
        flat=[{k:v for k,v in r.items() if k not in ("config","terminal","execution_diagnostics")} for r in values]
        pd.DataFrame(flat).to_csv(out/(key+".csv"),index=False,encoding="utf-8-sig",float_format="%.17g")
        repair["files"][key]=(out.name+"/"+key+".csv")
    save_json(out/"feasibility_repair.json",finite_json(repair))
    save_json(out/"failures.json",repair["failures"])
    progress("退出补充完成，"+str(len(repair["experiments"]))+"情景，失败"+str(len(repair["failures"])))
    return repair


def finalize_repair(study_path):
    """Publish no data: attach a successful independent audit and write reports."""
    study_path=Path(study_path)
    out=study_path/"feasibility_repair"
    repair=json.loads((out/"feasibility_repair.json").read_text(encoding="utf-8"))
    baseline=json.loads((study_path/"result.json").read_text(encoding="utf-8"))
    audit=json.loads((out/"feasibility_validation.json").read_text(encoding="utf-8"))
    if audit["status"]!="passed":
        raise ValueError("退出补充独立验收尚未通过")
    repair["validation"]={key:audit[key] for key in ("status","passed","total") if key in audit}
    repair["settlement_disclosure"]={"zero_residual_is_bookkeeping_only":True,
       "writeoffs_are_not_sales":True,"physical_settlement_verified":False,
       "note":"残仓为0可能包含连续缺价减记，不表示每项持仓均经卖出交收；原始WRITE_OFF账本、损失与现金流水全部保留。"}
    selected=repair["selected_id"]
    primary_choice=next(r for r in repair["selection_by_liquidation_only"] if r["experiment_id"]==selected and r["period_role"]=="evaluation_2025")
    primary=next((r for r in repair["experiments"] if r["experiment_id"]==selected and r["period_role"]=="evaluation_2025" and r["scenario"]==primary_choice["scenario"]),None)
    if primary:
        unit=json.loads((ROOT/"unit_validation_v3.json").read_text(encoding="utf-8")) if (ROOT/"unit_validation_v3.json").exists() else {}
        factor_audit=json.loads((ROOT/"research_strategy_validation.json").read_text(encoding="utf-8")) if (ROOT/"research_strategy_validation.json").exists() else {}
        unit_note=f"随附单元报告{unit.get('passed','未提供')}/{unit.get('tests','未提供')}，状态{unit.get('status','未提供')}；因子/未来扰动报告{factor_audit.get('checks','未提供')}项，passed={factor_audit.get('passed','未提供')}。这些既有代码核验记录的时间见各原JSON，不冒充本次批量运行重新执行。"
        save_json(out/"selected_execution_config.json",primary["config"])
        strategy=json.loads((study_path/"selected_strategy.json").read_text(encoding="utf-8"))
        save_json(out/"selected_execution_strategy.json",strategy)
        repair["files"].update(selected_execution_config=out.name+"/selected_execution_config.json",
                               selected_execution_strategy=out.name+"/selected_execution_strategy.json")
    lines=["# 退出可行性补充：20/40个交易日分批退出", "",
       "原始265情景已完成，最后一天才清仓导致大额残仓。本补充在看到该执行缺口后开展，属于后验修复；仍沿用2024选择的5个入围参数，不按2025收益更换因子或股票池。", "",
       "20先、40后只按账面是否零持仓且残值≤0.001元选择最短成功退出窗口，不按收益择时。该账面标准可能包含连续缺价减记，不能称所有持仓均已卖出交收。开始日在源交易日历上预排，未根据未来价格或成交额挑日子。", "",
       "## 2025原单日退出与分批退出", "",
       "| 2024入围参数 | 原始收益/残仓亿元 | 20日收益 | 20日残仓亿元 | 40日收益 | 40日残仓亿元 |", "|---|---:|---:|---:|---:|---:|"]
    for eid in repair["original_shortlist"]:
        old=next(r for r in baseline["experiments"] if r["experiment_id"]==eid and r["period_role"]=="evaluation_2025" and r["scenario"]=="base")
        a=next(r for r in repair["experiments"] if r["experiment_id"]==eid and r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down20")
        b=next(r for r in repair["experiments"] if r["experiment_id"]==eid and r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down40")
        lines.append(f"| {eid} | {old['total_return']:.4%} / {old['terminal_position_value']/1e8:.6f} | {a['total_return']:.4%} | {a['terminal_position_value']/1e8:.6f} | {b['total_return']:.4%} | {b['terminal_position_value']/1e8:.6f} |")
    lines += ["", "## 20日退出的费用与容量压力", "",
       "| 参数 | 基准收益/残仓亿元 | 加倍费用收益/残仓亿元 | 0.1%容量收益/残仓亿元 |", "|---|---:|---:|---:|"]
    for eid in repair["original_shortlist"]:
        values=[next(r for r in repair["experiments"] if r["experiment_id"]==eid and r["period_role"]=="evaluation_2025" and r["scenario"]==s)
                for s in ("wind_down20","wind_down20_stress","wind_down20_capacity_tight")]
        lines.append("| "+eid+" | "+" | ".join(f"{r['total_return']:.4%} / {r['terminal_position_value']/1e8:.6f}" for r in values)+" |")
    lines += ["", "## 作业延伸2025-01-02至2026-09-30", "",
       "| 参数 | 20日退出收益 | 最大回撤 | 末日残仓亿元 |", "|---|---:|---:|---:|"]
    for eid in repair["original_shortlist"]:
        r=next(r for r in repair["experiments"] if r["experiment_id"]==eid and r["period_role"]=="assignment_2025_2026sep" and r["scenario"]=="wind_down20")
        lines.append(f"| {eid} | {r['total_return']:.4%} | {r['max_drawdown']:.4%} | {r['terminal_position_value']/1e8:.6f} |")
    lines += ["",f"独立退出补充验收：{audit.get('passed')}/{audit.get('total')}，状态{audit['status']}。账本重建能确认模型中的现金和残仓，不能确认真实ST身份、开盘深度或实际交收。", "",
       "## 缺价减记不是成交", "", "下列金额已计入对应收益的损失；零账面残仓包含这些减记，不能解释成实际卖出完成。原包缺官方停牌、退市及换股公司行为资料，没有把减记伪装成成交。", "",
       "| 参数 | 2025提前20日退出的减记损失(元) |", "|---|---:|"]
    for r in repair["experiments"]:
        if r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down20":
            lines.append(f"| {r['experiment_id']} | {r['writeoff_loss']:,.2f} |")
    lines += ["",
       "## 复现", "", "精确复现2024冠军在2025的已清空模型，请成对使用selected_execution_config.json和selected_execution_strategy.json；原study根下selected_config/strategy保留为单日退出原对照。", "",
       "```powershell", f"python run_experiments.py --config {out.relative_to(study_path.parent.parent).as_posix()}/selected_execution_config.json --strategy {out.relative_to(study_path.parent.parent).as_posix()}/selected_execution_strategy.json", "```", "",
       "真实ST覆盖仍0，price_proxy不是真实非ST筛选。1%/0.1%当日最终成交额限制是事后安全情景，不是开盘撮合验证。收益和清仓结论均仅针对声明的合成总回报模型。", ""]
    (out/"report.md").write_text("\n".join(lines),encoding="utf-8")
    repair["files"]["report"]=out.name+"/report.md"
    save_json(out/"feasibility_repair.json",repair)
    if primary:
        reversal=next(r for r in repair["experiments"] if r["factor"]=="dr5_reversal" and r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down20")
        reversal_stress=next(r for r in repair["experiments"] if r["factor"]=="dr5_reversal" and r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down20_stress")
        reversal_tight=next(r for r in repair["experiments"] if r["factor"]=="dr5_reversal" and r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down20_capacity_tight")
        primary_stress=next(r for r in repair["experiments"] if r["experiment_id"]==selected and r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down20_stress")
        primary_tight40=next(r for r in repair["experiments"] if r["experiment_id"]==selected and r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down40_capacity_tight")
        report=["# 2025年10亿元选股研究：5DR反转有正收益，容量和退出规则决定可行性", "",
          f"仅使用用户指定feathers.zip，2025-01-02至12-31共243个源交易日。共10因子、120个候选，265个原始情景及35个后验退出修复情景；原始研究与失败对照全部保留。生成于{now()}（Asia/Shanghai）。", "",
          "## 主要结果", "",
          f"- **5DR反转**：流动性前1000范围、100只、周调仓、预定最后20交易日退出，2025总收益 **{reversal['total_return']:.4%}**，最大回撤 **{reversal['max_drawdown']:.4%}**，夏普{reversal['sharpe']:.4f}，模型末日账面残仓{reversal['terminal_position_value']:.2f}元；含缺价减记损失 **{reversal['writeoff_loss']:,.2f}元**，不是全部经卖出退出。参数来自该因子的2024代表，不是拿2025最高收益参数替换。",
          f"- **2024全局选型冠军：{primary['name']}**：流动性前1000、50只、月调仓、同样预定20日退出，2025收益 **{primary['total_return']:.4%}**，最大回撤 **{primary['max_drawdown']:.4%}**，夏普{primary['sharpe']:.4f}，模型末日账面残仓{primary['terminal_position_value']:.2f}元；含缺价减记损失 **{primary['writeoff_loss']:,.2f}元**。未以2025更高收益更换2024选型冠军。",
          f"- 5DR反转的加倍费用/滑点场景收益{reversal_stress['total_return']:.4%}；容量由1%收紧至0.1%时收益{reversal_tight['total_return']:.4%}，平均仓位{reversal_tight['mean_invested']:.2%}，模型末日残仓{reversal_tight['terminal_position_value']:.2f}元。",
          f"- {primary['name']}加倍费用/滑点收益{primary_stress['total_return']:.4%}；但容量0.1%时即使提前40日退出仍残留{primary_tight40['terminal_position_value']:,.2f}元，不能把这个容量压力场景称为已清空。",
          "- **真实历史ST状态已知覆盖0**。原ZIP没有名称、历史ST、官方每日限价、停牌或竞价深度。价格风险代理不是真实ST剔除，不能称本报告已经完全满足非ST全市场与真实开盘成交要求。", "",
          "## 先发现退出问题，再修复执行", "",
          "原始规则最后一天才尝试卖出，出现数亿元残仓。因此补充20/40个源交易日预定退出，逐日卖出并禁止新买，仍执行容量/停牌代理/限价代理等门禁；不强制伪造成交。窗口选择只看是否清空，20先、40后，不看收益。这个执行规则是在看到残仓后增加的，属于后验修复，不能包装为未触碰的确认性结果。", "",
          "低波动的两个入围股票池在20/40日退出后仍有残仓，均保留为不可按该窗口兑现的案例。5DR正向在20日退出情景亏损，未隐去。", "",
          "## 2024入围参数的2025分批退出结果", "",
          "| 策略及股票池 | 持股/调仓 | 20日退出收益 | 最大回撤 | 平均仓位 | 末日账面残仓(元) | 缺价减记损失(元) |", "|---|---|---:|---:|---:|---:|---:|"]
        for r in repair["experiments"]:
            if r["period_role"]=="evaluation_2025" and r["scenario"]=="wind_down20":
                report.append(f"| {r['name']} / {r['universe_name']} | {r['top_n']}/{r['rebalance']} | {r['total_return']:.4%} | {r['max_drawdown']:.4%} | {r['mean_invested']:.2%} | {r['terminal_position_value']:,.2f} | {r['writeoff_loss']:,.2f} |")
        report += ["", "## 其余因子也已测试：全部原始对照", "",
          "下表每项参数由2024选择。收益是原单日退出规则的收盘估值收益，残仓不等于现金，不能把它直接当成已全部兑现；完整120候选见experiments.csv。", "",
          "| 因子 | 2025估值收益 | 最大回撤 | 末日残仓(元) |", "|---|---:|---:|---:|"]
        reps=set(baseline["selection"]["factor_representatives"].values())
        for r in baseline["experiments"]:
            if r["experiment_id"] in reps and r["period_role"]=="evaluation_2025" and r["scenario"]=="base":
                report.append(f"| {r['name']} | {r['total_return']:.4%} | {r['max_drawdown']:.4%} | {r['terminal_position_value']:,.2f} |")
        report += ["", "## 费用、时序与数值精度", "",
          "作业资金10亿元，单边佣金1bp、卖出印花税5bp；额外公开最低佣金5元、双边过户费0.1bp、目标滑点5bp及不利0.01元刻度舍入。佣金/税费/容量均按真实本模型成交价×等价股数计算。前日收盘冻结目标、次日开盘尝试，先卖后买、整手/T+1、现金非负、日容量累计、公司行为跳变拒单及末日残仓留痕。", "",
          f"原始研究独立验收{baseline['validation']['passed']}/{baseline['validation']['total']}，重建{baseline['validation'].get('detailed_ledgers','见验收JSON')}组完整账本；退出补充验收{audit.get('passed')}/{audit.get('total')}。基线最大NAV金额误差{baseline['validation'].get('precision_summary',{}).get('daily_nav',float('nan')):.9g}元，固定容差0.001元（0.1分），未放宽。{unit_note} CSV以17位有效数字保存；这些精度仅针对声明模型，不代表市场撮合精度。", "",
          "## 不能从当前数据确认的部分", "",
          "仅cumadj无法分离分红现金、送转、税和到账，持仓为合成总回报份额。amt没有明确货币单位元数据，以元解释是公开假设。当日最终成交额、OHLC与因子跳变用于事后安全审计，不能证明开盘可得深度。理论涨跌停和主板未知ST的5%门禁不是官方每日限价。", "",
          "2025在V1已经被部分观察，2024选型仍是同源回顾性比较。原始冠军与同规则高成交额对照的比较、固定组合区块重采样及原始P见原报告；退出修复后的收益不能与不同退出规则对照直接混称alpha。没有额外独立样本、真实ST或逐笔证据，不能证明未来实盘盈利。", "",
          "## 可复核文件", "",
          f"- [原始265情景报告](research_outputs/{baseline['study_id']}/report.md)",
          f"- [35情景退出修复与压力报告](research_outputs/{baseline['study_id']}/feasibility_repair/report.md)",
          f"- [全部120候选及原始情景CSV](research_outputs/{baseline['study_id']}/experiments.csv)",
          f"- [退出修复CSV](research_outputs/{baseline['study_id']}/feasibility_repair/experiments.csv)",
          f"- [原始独立验收](research_outputs/{baseline['study_id']}/validation.json) / [退出补充独立验收](research_outputs/{baseline['study_id']}/feasibility_repair/feasibility_validation.json)",
          "- [数据边界审计](metadata/PACKAGE_ONLY_AUDIT.md) / [V3使用指南](V3_GUIDE.md)", ""]
        (ROOT/"REPORT_2025.md").write_text("\n".join(report),encoding="utf-8")
    return repair


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("study",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--finalize",action="store_true")
    args=parser.parse_args()
    if args.finalize:
        print(json.dumps({"status":finalize_repair(args.study)["validation"]},ensure_ascii=False))
    else:
        run_repair(args.study,args.output,lambda s:print(s,flush=True))
