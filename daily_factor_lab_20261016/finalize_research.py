"""Attach independent audit evidence and a matched executable-strategy comparator.

No simulation, parameter search, new data download, or external publication.
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from research import ROOT, RESEARCH_ROOT, save_json, write_report, now


def finalize(directory):
    directory=Path(directory)
    study=json.loads((directory/"result.json").read_text(encoding="utf-8"))
    audit=json.loads((directory/"validation.json").read_text(encoding="utf-8"))
    if audit["status"]!="passed":
        raise ValueError("独立验收未通过，不能标记已验证")
    precision={}
    for ledger in audit["detailed_ledgers"]:
        for key,value in ledger["errors"].items():
            precision[key]=max(precision.get(key,0.),float(value))
    study["validation"]={key:audit[key] for key in ("status","passed","total","scope","money_absolute_tolerance")}
    non_money={"daily_return","daily_turnover","participation_record","raw_share_from_adjusted_units","terminal_units"}
    study["validation"].update(validated_at=now(),detailed_ledgers=len(audit["detailed_ledgers"]),
                              precision_summary={k:v for k,v in precision.items() if k not in non_money},
                              other_numeric_errors={k:v for k,v in precision.items() if k in non_money})
    study["files"]["validation"]=study["study_id"]+"/validation.json"
    selected_id=study["selection"]["selected_id"]
    selected=next(r for r in study["experiments"] if r["experiment_id"]==selected_id and r["period_role"]=="evaluation_2025" and r["scenario"]=="base")
    benchmark_id=f"liquidity20__{selected['universe']}__n{selected['top_n']}__{selected['rebalance']}"
    benchmark=next(r for r in study["experiments"] if r["experiment_id"]==benchmark_id and r["period_role"]=="evaluation_2025" and r["scenario"]=="base")
    def daily(eid):
        return pd.read_csv(directory/"runs"/eid/"evaluation_2025"/"base"/"daily.csv",float_precision="round_trip")
    a,b=daily(selected_id),daily(benchmark_id)
    if not a.date.equals(b.date):
        raise ValueError("同模型对照日历不一致")
    rng=np.random.default_rng(20251016)
    count,block,reps=len(a),10,2000
    starts=rng.integers(0,count,(reps,math.ceil(count/block)))
    idx=((starts[:,:,None]+np.arange(block))%count).reshape(reps,-1)[:,:count]
    sampled_a=np.prod(1+a["return"].to_numpy()[idx],axis=1)-1
    sampled_b=np.prod(1+b["return"].to_numpy()[idx],axis=1)-1
    difference=sampled_a-sampled_b
    study["benchmark_comparison"]={"selected_id":selected_id,"benchmark_id":benchmark_id,
       "definition":"同股票池/持股数/调仓频率的高成交额组合，同一引擎含费可行性情景；非官方指数",
       "selected_total_return":selected["total_return"],"benchmark_total_return":benchmark["total_return"],
       "total_return_difference":selected["total_return"]-benchmark["total_return"],
       "difference_ci95_low":float(np.quantile(difference,.025)),"difference_ci95_high":float(np.quantile(difference,.975)),
       "method":"paired circular block bootstrap; fixed selected and comparator portfolios; no reselection",
       "block_length":block,"repetitions":reps,"seed":20251016,
       "status":"post-selection descriptive comparator, not an additional model selection criterion"}
    study["requirements_status"]={"capital_and_commission_stamp":"implemented",
       "true_non_ST_filter":"not_verifiable_with_archive_only",
       "auction_fill_feasibility":"not_verifiable_with_daily_data",
       "terminal_cash_only":"see actual residual for each experiment",
       "high_precision_accounting":"independently reconstructed within stated tolerance under synthetic total-return model"}
    study["files"].update(curves=study["study_id"]+"/curves.csv",splits=study["study_id"]+"/splits.csv",
                          execution=study["study_id"]+"/execution.csv",st_coverage=study["study_id"]+"/st_coverage.csv")
    supplement=directory/"uncertainty_protocol_supplement.json"
    if supplement.exists():
        study["protocol"]["uncertainty_supplement"]=json.loads(supplement.read_text(encoding="utf-8-sig"))
        study["files"]["uncertainty_protocol_supplement"]=study["study_id"]+"/uncertainty_protocol_supplement.json"
    save_json(directory/"result.json",study)
    write_report(directory,study)
    return {"study_id":study["study_id"],"validation":study["validation"],"benchmark_comparison":study["benchmark_comparison"]}


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("study",type=Path)
    args=parser.parse_args()
    print(json.dumps(finalize(args.study),ensure_ascii=False,indent=2,allow_nan=False))
