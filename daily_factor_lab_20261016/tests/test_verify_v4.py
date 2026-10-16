"""Analytical persisted-file fixtures and corruption tests; no simulations."""
import os
for _key in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMEXPR_NUM_THREADS","ARROW_NUM_THREADS"):
    os.environ[_key]="1"
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import shutil
import uuid
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import verify_v4 as audit
from research import reserve_performance_cores
RESOURCE_POLICY=reserve_performance_cores()
pa.set_cpu_count(1);pa.set_io_thread_count(1)


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding="utf-8")


def fixture(out):
    calendar=list(pd.bdate_range("2024-01-02",periods=20))+list(pd.bdate_range("2025-01-02",periods=20))
    table=pa.table({"date":pd.DatetimeIndex([pd.Timestamp("2023-12-29")]+calendar)})
    buffer=pa.BufferOutputStream();feather.write_feather(table,buffer)
    source=out/"source.zip"
    with zipfile.ZipFile(source,"w") as archive:archive.writestr("feathers/close.feather",buffer.getvalue().to_pybytes())
    cfg={"capital":1e6,"top_n":50,"rebalance":"monthly","min_history":120,"universe":"all_a","liquidity_top_n":0,
         "max_participation":.01,"max_daily_participation":.01,"wind_down_days":20,"liquidate_end":True}
    candidate={"experiment_id":"demo","factor_id":"demo_factor","idea_id":"demo_idea","universe":"all_liquid","top_n":50,"rebalance":"monthly","required_history":20}
    protocol={"grid":[candidate],"candidate_count":1,"planned_primary_runs":4,"maximum_numeric_money_tolerance":.001,
              "retrospective":True,"st_truth_available":False,"opening_auction_verified":False,"frozen_at":"2026-01-01T00:00:00+08:00",
              "source_identity":{"path":str(source.resolve()),"size_bytes":source.stat().st_size,"sha256":hashlib.sha256(source.read_bytes()).hexdigest()},
              "code_sha256":{name:hashlib.sha256((audit.ROOT/name).read_bytes()).hexdigest() for name in audit.REQUIRED_CODE},
              "periods":{"selection_2024":["2024-01-02",str(calendar[19].date())],"evaluation_2025":["2025-01-02",str(calendar[-1].date())]},
              "base_config":cfg,"wind_down_days_frozen_before_this_run":20,"universes":{"all_liquid":{"universe":"all_a","liquidity_top_n":0}}}
    selection={"selected_on":"selection_2024","selected_id":None,"shortlist":[],"audit_ids":[],"admissible_count":0,"no_fallback":True,
               "frozen_at":"2026-01-01T00:00:02+08:00","training_unique_signal_signatures":1,"training_equivalent_groups":[]}
    records=[]
    aggregate=copy.deepcopy(candidate)
    field_map={("selection_2024","base"):"selection_2024",("evaluation_2025","base"):"evaluation_2025",("evaluation_2025","stress"):"stress_2025",("evaluation_2025","capacity"):"capacity_2025"}
    for role,scenario in audit.PRIMARY:
        config={**cfg,"start":protocol["periods"][role][0],"end":protocol["periods"][role][1]}
        if scenario=="capacity":config.update(max_participation=.001,max_daily_participation=.001)
        dates=calendar[:20] if role=="selection_2024" else calendar[20:]
        daily=pd.DataFrame({"date":[str(d.date()) for d in dates],"nav":1e6,"cash":1e6,"return":0.,"cost":0.,"turnover":0.,
                            "invested_fraction":0.,"writeoff_loss":0.,"stale_fraction":0.,"daily_capacity_used":0.,"daily_capacity_max_participation":0.,
                            "positions":0,"trades":0,"blocked_attempts":0,"wind_down":True,"terminal_liquidation":[False]*19+[True]})
        metrics,_=audit.metrics_from_daily(daily,1e6);metrics["capacity_excess"]=0.
        folder=f"runs/demo/{role}/{scenario}"
        daily_path=out/folder/"daily.csv";daily_path.parent.mkdir(parents=True,exist_ok=True);daily.to_csv(daily_path,index=False,float_format="%.17g")
        row={"experiment_id":"demo","factor_id":"demo_factor","period_role":role,"scenario":scenario,"folder":folder,"status":"success",
             "config":config,"metrics":metrics,"cost_multiplier":2. if scenario=="stress" else 1.,"blocked":{},
             "started_at":"2026-01-01T00:00:00.5+08:00" if role=="selection_2024" else "2026-01-01T00:00:03+08:00",
             "completed_at":"2026-01-01T00:00:01+08:00" if role=="selection_2024" else "2026-01-01T00:00:04+08:00",
             "terminal":{"model_position_zero":True,"fully_exited_by_trades":True,"residual_value":0.,"writeoff_loss":0.,"residual_positions":0,
                         "wind_down_start":str(dates[0].date()),"wind_down_days":20},
             "execution_diagnostics":{"max_combined_capacity_excess":0.,"max_realized_daily_participation":0.,"historical_st_known_observations":0,
                                      "opening_liquidity_verified":False,"posthoc_not_opening_information":True}}
        if role=="selection_2024":row["signal_diagnostics"]={"training_signal_signature":"b"*64}
        save(out/folder/"record.json",row);records.append(row)
        aggregate[field_map[(role,scenario)]]={"status":"success","folder":folder,**metrics}
    study={"protocol":protocol,"candidates":[aggregate],"selection":selection,"detailed_records":[]}
    save(out/"protocol.json",protocol);save(out/"selection_frozen_before_2025.json",selection)
    frozen_time=pd.Timestamp(selection["frozen_at"]).timestamp()
    os.utime(out/"selection_frozen_before_2025.json",(frozen_time,frozen_time))
    return source,study,records


class V4PersistedValidationTests(unittest.TestCase):
    def setUp(self):
        # Python3.14 tempfile's restrictive Windows ACL is inaccessible in this
        # managed token; ordinary workspace mkdir retains usable inherited ACLs.
        self.out=audit.ROOT/"tests"/("verify_fixture_"+uuid.uuid4().hex)
        self.out.mkdir()
        def cleanup():
            resolved=self.out.resolve();resolved.relative_to((audit.ROOT/"tests").resolve())
            shutil.rmtree(resolved)
        self.addCleanup(cleanup)
        self.source,self.study,self.records=fixture(self.out)
        self.patch=patch.object(audit,"SOURCE",self.source);self.patch.start();self.addCleanup(self.patch.stop)

    def validate(self):return audit.validate_study(self.out,self.study,self.records)

    def test_analytical_four_run_grid_passes_without_simulation(self):
        report=self.validate()
        self.assertTrue(report["passed"],report)
        self.assertEqual(len(report["passed_run_keys"]),4)
        self.assertEqual(report["money_absolute_tolerance"],.001)
        self.assertEqual(report["detailed_ledgers"],[])

    def test_in_memory_tuple_periods_match_persisted_json_arrays(self):
        self.study["protocol"]["periods"]={k:tuple(v) for k,v in self.study["protocol"]["periods"].items()}
        self.assertNotEqual(audit.load(self.out/"protocol.json"),self.study["protocol"])
        report=self.validate()
        self.assertTrue(report["passed"],report)
        self.assertTrue(next(c for c in report["checks"] if c["name"]=="frozen_protocol_matches_disk")["passed"])

    def test_tuple_normalization_preserves_value_precision_keys_and_sequence_order(self):
        persisted=audit.load(self.out/"protocol.json")
        original=self.study["protocol"]
        for change in ("date","numeric","hash","missing","order"):
            with self.subTest(change=change):
                altered=copy.deepcopy(original)
                altered["periods"]={k:tuple(v) for k,v in altered["periods"].items()}
                if change=="date":altered["periods"]["evaluation_2025"]=("2025-01-03",altered["periods"]["evaluation_2025"][1])
                elif change=="numeric":altered["maximum_numeric_money_tolerance"]+=1e-15
                elif change=="hash":altered["code_sha256"]["lab.py"]="0"*64
                elif change=="missing":del altered["retrospective"]
                else:altered["periods"]["evaluation_2025"]=tuple(reversed(altered["periods"]["evaluation_2025"]))
                self.assertNotEqual(persisted,audit.json_arrays(altered))

    def test_nav_corruption_is_not_hidden_by_record_summary(self):
        path=self.out/self.records[-1]["folder"]/"daily.csv"
        data=pd.read_csv(path);data.loc[5,"nav"]+=10.;data.to_csv(path,index=False,float_format="%.17g")
        report=self.validate()
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["passed_run_keys"]),3)

    def test_failed_run_and_duplicate_coverage_are_not_discarded(self):
        self.records[-1]["status"]="failed"
        report=self.validate();self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["failed_runs"],1)
        self.records.append(copy.deepcopy(self.records[0]))
        self.assertFalse(self.validate()["passed"])

    def test_evaluation_before_training_freeze_is_detected(self):
        self.study["selection"]["frozen_at"]="2026-01-01T00:00:05+08:00"
        save(self.out/"selection_frozen_before_2025.json",self.study["selection"])
        report=self.validate();self.assertFalse(report["passed"])
        self.assertTrue(any(not c["passed"] and c["name"]=="training_completed_before_selection_and_evaluation" for c in report["checks"]))

    def test_missing_detailed_group_is_an_explicit_failure(self):
        self.study["detailed_records"]=[{"status":"failed","scenario":"detailed_group","error":"worker died"}]
        report=self.validate();self.assertFalse(report["passed"])
        self.assertEqual(len(report["detailed_ledgers"]),1)
        self.assertFalse(report["detailed_ledgers"][0]["passed"])

    def test_fatal_input_error_is_persisted_as_failed_json(self):
        (self.out/"protocol.json").unlink()
        report=self.validate();self.assertFalse(report["passed"])
        self.assertEqual(json.loads((self.out/"validation.json").read_text())["status"],"failed")


if __name__=="__main__":unittest.main()
