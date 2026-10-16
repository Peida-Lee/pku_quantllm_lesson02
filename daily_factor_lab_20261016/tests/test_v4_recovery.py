"""Mock-only recovery/provenance tests. Never read a live queue or run a model."""
import os
for _key in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMEXPR_NUM_THREADS","ARROW_NUM_THREADS"):
    os.environ[_key]="1"

import copy
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

import numpy as np
import pandas as pd
import pyarrow as pa

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import research_v4 as runner
import verify_v4

pa.set_cpu_count(1);pa.set_io_thread_count(1)
RESOURCE_POLICY=runner.reserve_performance_cores()


def candidate(identity="demo",factor="reversal_w005"):
    return {"experiment_id":identity,"factor_id":factor,"factor":factor,"idea_id":factor,
            "universe":"all_liquid","top_n":50,"rebalance":"monthly","name":"Mock only"}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).resolve().parent/("recovery_fixture_"+uuid.uuid4().hex)
        self.root.mkdir()
        def cleanup():
            path=self.root.resolve();path.relative_to(Path(__file__).resolve().parent)
            shutil.rmtree(path)
        self.addCleanup(cleanup)
        self.dates=pd.bdate_range("2025-01-02",periods=25)
        self.cfg=runner.base_config(start=str(self.dates[0].date()),end=str(self.dates[-1].date()),capital=10000.,wind_down_days=3)
        self.daily=pd.DataFrame({"date":self.dates.strftime("%Y-%m-%d"),"nav":10000.,"cash":10000.,"return":0.})
        self.market=SimpleNamespace(dates=self.dates,eligible=MagicMock(return_value=np.ones((25,2),bool)))
        self.engine=MagicMock();self.engine.compute.return_value=np.ones((25,2))
        self.simulation={"daily":self.daily,"metrics":{"writeoff_loss":0.,"total_return":0.},
                         "holdings":pd.DataFrame(columns=["symbol","value"]),"trades":pd.DataFrame(columns=["date","side"]),
                         "orders":pd.DataFrame(columns=["date","status"]),"terminal":{"residual_value":0.},"blocked":{},
                         "execution_diagnostics":{"max_combined_capacity_excess":0.}}
        self.worker_patch=patch.dict(runner._WORKER,market=self.market,engine=self.engine,out=self.root,protocol={})
        self.worker_patch.start();self.addCleanup(self.worker_patch.stop)
        self.cfg_patch=patch.object(runner,"config_for",return_value=self.cfg);self.cfg_patch.start();self.addCleanup(self.cfg_patch.stop)
        self.sim_patch=patch.object(runner,"simulate",return_value=self.simulation);self.sim=self.sim_patch.start();self.addCleanup(self.sim_patch.stop)

    def path(self,c=None,detailed=False):
        return runner._record_path(self.root,c or candidate(),"evaluation_2025","base",detailed)/"record.json"

    def test_success_checkpoint_skips_recalculation(self):
        first=runner._run_one(candidate(),"evaluation_2025","base")
        second=runner._run_one(candidate(),"evaluation_2025","base",retry_failed=True)
        self.assertEqual(first["status"],"success")
        self.assertEqual(first,second)
        self.assertEqual(self.sim.call_count,1)
        self.assertTrue(self.path().is_file())

    def test_failed_checkpoint_requires_explicit_retry(self):
        self.sim.side_effect=RuntimeError("mock model failure")
        first=runner._run_one(candidate(),"evaluation_2025","base")
        self.assertEqual(first["status"],"failed")
        self.sim.side_effect=None
        skipped=runner._run_one(candidate(),"evaluation_2025","base")
        self.assertEqual(skipped["status"],"failed")
        self.assertEqual(self.sim.call_count,1)
        retried=runner._run_one(candidate(),"evaluation_2025","base",retry_failed=True)
        self.assertEqual(retried["status"],"success")
        self.assertEqual(self.sim.call_count,2)

    def test_bad_json_is_preserved_and_only_retried_explicitly(self):
        path=self.path();path.parent.mkdir(parents=True);original="{broken checkpoint"
        path.write_text(original,encoding="utf-8")
        failure=runner._run_one(candidate(),"evaluation_2025","base")
        self.assertEqual(failure["status"],"failed")
        preserved=list(path.parent.glob("record.corrupt_*.json"))
        self.assertEqual(len(preserved),1)
        self.assertEqual(preserved[0].read_text(encoding="utf-8"),original)
        self.sim.assert_not_called()
        self.assertEqual(runner._run_one(candidate(),"evaluation_2025","base",retry_failed=True)["status"],"success")

    def test_missing_daily_or_detailed_file_cannot_skip_as_success(self):
        for detailed,missing in ((False,"daily.csv"),(True,"orders.csv")):
            c=candidate("detail" if detailed else "daily")
            good=runner._run_one(c,"evaluation_2025","base",detailed=detailed)
            self.assertEqual(good["status"],"success")
            path=self.path(c,detailed);(path.parent/missing).unlink()
            before=self.sim.call_count
            failed=runner._run_one(c,"evaluation_2025","base",detailed=detailed)
            self.assertEqual(failed["status"],"failed")
            self.assertEqual(self.sim.call_count,before)
            self.assertEqual(len(list(path.parent.glob("record.corrupt_*.json"))),1)
            self.assertEqual(runner._run_one(c,"evaluation_2025","base",detailed=detailed,retry_failed=True)["status"],"success")

    def test_csv_write_failure_persists_failed_status_instead_of_success(self):
        with patch.object(pd.DataFrame,"to_csv",side_effect=OSError("mock disk full")):
            record=runner._run_one(candidate(),"evaluation_2025","base")
        self.assertEqual(record["status"],"failed")
        self.assertIn("mock disk full",record["error"])
        self.assertEqual(json.loads(self.path().read_text(encoding="utf-8"))["status"],"failed")

    def test_one_simulation_failure_continues_remaining_candidates_and_scenarios(self):
        self.sim.side_effect=[RuntimeError("first failed")]+[self.simulation]*5
        result=runner._factor_group(("reversal_w005","evaluation_2025",[candidate("first"),candidate("second")],False))
        self.assertEqual(len(result),6)
        self.assertEqual(result[0]["status"],"failed")
        self.assertTrue(all(r["status"]=="success" for r in result[1:]))
        self.assertEqual(self.sim.call_count,6)

    def test_checkpoint_wrong_identity_is_not_accepted(self):
        runner._run_one(candidate(),"evaluation_2025","base")
        path=self.path();data=json.loads(path.read_text(encoding="utf-8"));data["experiment_id"]="someone_else"
        path.write_text(json.dumps(data),encoding="utf-8")
        before=self.sim.call_count
        self.assertEqual(runner._run_one(candidate(),"evaluation_2025","base")["status"],"failed")
        self.assertEqual(before,self.sim.call_count)

    def test_corrupted_existing_daily_is_left_for_independent_validator(self):
        # Document the current boundary: existence is checked at resume, full
        # numeric content is the independent validator's job. No silent pass is
        # claimed by this test; production numeric acceptance remains separate.
        runner._run_one(candidate(),"evaluation_2025","base")
        (self.path().parent/"daily.csv").write_text("not a daily matrix",encoding="utf-8")
        before=self.sim.call_count
        row=runner._run_one(candidate(),"evaluation_2025","base",retry_failed=True)
        self.assertEqual(row["status"],"success")
        self.assertEqual(before,self.sim.call_count)
        self.assertNotIn("numeric_validated",row)


class FreezeAndSelectionTests(unittest.TestCase):
    def test_config_uses_frozen_pool_dates_and_parameters(self):
        c=candidate()
        frozen={"base_config":asdict(runner.base_config(commission=.0002,wind_down_days=40)),
                "periods":{"evaluation_2025":["2025-02-03","2025-11-28"]},
                "universes":{"all_liquid":{"universe":"main_a","liquidity_top_n":17}}}
        with patch.object(runner,"PERIODS",{}),patch.object(runner,"UNIVERSES",{}):
            config=runner.config_for(c,"evaluation_2025","capacity",frozen)
        self.assertEqual((config.start,config.end),("2025-02-03","2025-11-28"))
        self.assertEqual((config.universe,config.liquidity_top_n),("main_a",17))
        self.assertEqual((config.commission,config.wind_down_days),(.0002,40))
        self.assertEqual((config.max_participation,config.max_daily_participation),(.001,.001))

    def test_only_requested_original_zip_is_accepted(self):
        with patch.object(runner,"SOURCE",Path(__file__).resolve().parent/"unapproved_source.zip"):
            with self.assertRaisesRegex(ValueError,"仅允许"):
                runner.source_identity()

    def test_complete_H1_H2_required_for_positive_halves(self):
        base={"status":"success","writeoff_loss":0.,"exit_by_trades":True,"total_return":.1,"max_drawdown":-.1,"mean_invested":.5}
        c={name:copy.deepcopy(base) for name in ("selection_2024","evaluation_2025","stress_2025","capacity_2025")}
        for halves,expected in (({"H1":.1},False),({"H2":.1},False),({"H1":.1,"H2":-.01},False),({"H1":.1,"H2":.1,"H3":.1},False),({"H1":.1,"H2":.1},True)):
            c["evaluation_2025"]["half_returns"]=halves
            self.assertEqual(runner.classify(copy.deepcopy(c))["checks"]["positive_halves"],expected)

    def test_training_basket_signature_is_affine_invariant_and_ignores_future(self):
        dates=pd.bdate_range("2024-01-01",periods=280)
        m=SimpleNamespace(dates=dates,symbols=np.array(["000001.SZ","000002.SZ","000003.SZ"]),
                          eligible=lambda cfg:np.ones((len(dates),3),bool))
        cfg=runner.base_config(start="2024-01-02",end="2024-12-31",top_n=2,wind_down_days=20)
        score=np.tile([3.,2.,1.],(len(dates),1))
        a=runner.signal_signature(m,score,cfg)
        transformed=2*score+7;transformed[dates.year==2025]=[100.,200.,300.]
        self.assertEqual(a["training_signal_signature"],runner.signal_signature(m,transformed,cfg)["training_signal_signature"])
        changed=score.copy();changed[:,0]=0
        self.assertNotEqual(a["training_signal_signature"],runner.signal_signature(m,changed,cfg)["training_signal_signature"])
        # Same equal-weight basket, reversed priority: same basket signature,
        # explicitly not a claim that limited-cash fills would be identical.
        all_cfg=runner.base_config(start="2024-01-02",end="2024-12-31",top_n=3,wind_down_days=20)
        same=runner.signal_signature(m,score,all_cfg)
        reverse=runner.signal_signature(m,-score,all_cfg)
        self.assertEqual(same["training_signal_signature"],reverse["training_signal_signature"])
        self.assertIn("does not establish identical fills",same["signature_definition"])

    @unittest.expectedFailure
    def test_choose_training_defensively_rejects_mixed_evaluation_records(self):
        # Known defensive API gap: production caller currently passes only the
        # completed training phase. This expected failure records the hazard if
        # another caller passes a mixed list; no live computation is changed.
        c=[candidate("a"),candidate("b")]
        def record(eid,role,sharpe):
            return {"experiment_id":eid,"period_role":role,"status":"success",
                    "metrics":{"mean_invested":.5,"max_drawdown":-.1,"total_return":.1,"exit_by_trades":True,"sharpe":sharpe},
                    "signal_diagnostics":{"training_signal_signature":eid}}
        data=[record("a","selection_2024",2),record("b","selection_2024",1),record("b","evaluation_2025",100)]
        self.assertEqual(runner.choose_training(c,data)["selected_id"],"a")


class PilotAndResumeTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).resolve().parent/("recovery_control_"+uuid.uuid4().hex);self.root.mkdir()
        def cleanup():
            path=self.root.resolve();path.relative_to(Path(__file__).resolve().parent);shutil.rmtree(path)
        self.addCleanup(cleanup)

    def test_pilot_finalize_never_overwrites_production_latest(self):
        pointer=self.root/"latest.json";pointer.write_text('{"study_id":"real_previous"}',encoding="utf-8")
        study={"protocol":{"pilot":True},"candidates":[],"failures":[],"curves":[]}
        validation={"passed":True,"passed_run_keys":[],"status":"passed"}
        with patch.object(runner,"LATEST",pointer),patch.object(verify_v4,"validate_study",return_value=validation),patch.object(runner,"write_report"):
            result=runner.finalize(self.root,study,[])
        self.assertTrue(result["snapshot_ready"])
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")),{"study_id":"real_previous"})

    def test_resume_rejects_changed_compute_hash_before_creating_workers(self):
        out=self.root/"study";out.mkdir()
        (out/"protocol.json").write_text(json.dumps({"code_sha256":{"lab.py":"old"}}),encoding="utf-8")
        with patch.object(runner,"OUTPUT",self.root),patch.object(runner,"LOCK_PATH",self.root/"lock.json"),patch.object(runner,"code_hashes",return_value={"lab.py":"new"}),patch.object(runner,"ProcessPoolExecutor") as pool,patch.object(runner,"reserve_performance_cores",return_value={}):
            with self.assertRaisesRegex(RuntimeError,"冻结协议"):
                runner.run_study(workers=1,resume=out,progress=lambda *_:None)
            pool.assert_not_called()
        self.assertTrue((out/"interruption.json").is_file())
        self.assertFalse((self.root/"lock.json").exists())

    def test_resume_rejects_changed_source_identity_before_workers(self):
        out=self.root/"study";out.mkdir()
        (out/"protocol.json").write_text(json.dumps({"code_sha256":{"lab.py":"same"},"source_identity":{"sha256":"old"}}),encoding="utf-8")
        with patch.object(runner,"OUTPUT",self.root),patch.object(runner,"LOCK_PATH",self.root/"lock.json"),patch.object(runner,"code_hashes",return_value={"lab.py":"same"}),patch.object(runner,"source_identity",return_value={"sha256":"new"}),patch.object(runner,"ProcessPoolExecutor") as pool,patch.object(runner,"reserve_performance_cores",return_value={}):
            with self.assertRaisesRegex(RuntimeError,"ZIP身份"):
                runner.run_study(workers=1,resume=out,progress=lambda *_:None)
            pool.assert_not_called()


if __name__=="__main__":unittest.main()
