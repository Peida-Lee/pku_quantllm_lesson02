"""Compare the actual 1400-day block strategy against the equivalent V1 rule."""
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
V1=ROOT/"outputs"/"run_20261016_194950_230679"
V2=ROOT/"outputs"/"run_20261016_201056_106039"

def main():
    report={"run_id":V2.name,"v1_run_id":V1.name,"scenarios":[],"success":True}
    for scenario in ["base","gross","stress"]:
        a=pd.read_csv(V1/scenario/"lowvol20"/"daily.csv")
        b=pd.read_csv(V2/scenario/"custom"/"daily.csv")
        pd.testing.assert_frame_equal(a,b,check_exact=True)
        row={"scenario":scenario,"trading_days":len(a),"all_daily_accounting_columns_exactly_equal":True,
             "max_nav_difference":float(np.max(np.abs(a.nav-b.nav))),"max_cash_difference":float(np.max(np.abs(a.cash-b.cash)))}
        if scenario=="base":
            pd.testing.assert_frame_equal(pd.read_csv(V1/scenario/"lowvol20"/"trades.csv"),pd.read_csv(V2/scenario/"custom"/"trades.csv"),check_exact=True)
            pd.testing.assert_frame_equal(pd.read_csv(V1/scenario/"lowvol20"/"holdings.csv"),pd.read_csv(V2/scenario/"custom"/"holdings.csv"),check_exact=True)
            row.update({"all_trade_ledger_fields_exactly_equal":True,"all_final_holdings_exactly_equal":True})
        report["scenarios"].append(row)
    a=pd.read_csv(V1/"ic_daily.csv").query("factor=='lowvol20'").drop(columns=["factor","name"]).reset_index(drop=True)
    b=pd.read_csv(V2/"ic_daily.csv").drop(columns=["factor","name"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a,b,check_exact=True)
    report["all_daily_ic_and_pair_coverage_exactly_equal"]=True
    (ROOT/"v2_engine_equivalence.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report))

if __name__=="__main__": main()
