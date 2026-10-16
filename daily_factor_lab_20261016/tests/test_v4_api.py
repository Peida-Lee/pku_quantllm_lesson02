"""Isolated loopback HTTP tests. Threads/compute/subprocesses are mocked.

Never connects to the user's running server or reads a live study/progress file.
All writes occur in TemporaryDirectory; no Market or real research is started.
"""
import os
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_name] = "1"

from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import asdict
from datetime import datetime, timedelta
import http.client
import json
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pyarrow as pa
import research
import research_v4 as rv4
import server as api

pa.set_cpu_count(1)
pa.set_io_thread_count(1)


class QuietHandler(api.Handler):
    def log_message(self, *args):
        pass


class V4HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        resource = research.reserve_performance_cores()
        if os.name == "nt" and len(resource.get("reserved_logical_processors", [])) < 2:
            raise RuntimeError("Two performance cores could not be reserved")
        cls.resource = resource
        cls.httpd = api.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.serving = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.serving.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.serving.join(timeout=3)

    def setUp(self):
        # Python 3.14 tempfile uses a Windows owner-only 0700 ACL, which removes
        # this sandbox token's inherited group access. Use a normal workspace
        # directory with inherited ACLs instead, never alter user/system ACLs.
        self.root = Path(__file__).resolve().parent / ("_v4_api_fixture_"+uuid.uuid4().hex)
        self.root.mkdir()
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.patchers = []
        self.jobs = []
        self.saved_jobs = set(api.LOCAL_JOBS)
        api.LOCAL_JOBS.clear()
        self.add_patch(patch.dict(api.STATE, {"busy":False,"progress":"就绪","error":None,"latest_run":None,
                                             "job_id":None,"job_kind":None,"status":"idle"}, clear=True))
        for name, value in (("OUTPUTS",self.outputs),("JOB_FILE",self.outputs/"server_job.json"),
                            ("STRATEGIES",self.root/"strategies"),("PACKS",self.root/"packs"),("MARKET",None)):
            self.add_patch(patch.object(api, name, value))
        self.active = self.add_patch(patch.object(rv4,"active_v4_running",return_value=False))
        self.progress = self.add_patch(patch.object(rv4,"progress_v4",return_value={"status":"not_run","completed":0,"total":5616}))
        self.hashes = {"frozen_compute.py":"mock-version-1"}
        self.add_patch(patch.object(rv4,"code_hashes",return_value=self.hashes))
        self.source_identity = {"path":"D:/quant/feathers.zip","size_bytes":159096263,"sha256":"mock-source-identity"}
        self.source = self.add_patch(patch.object(rv4,"source_identity",return_value=self.source_identity))
        self.study = self.valid_study()
        self.latest = self.add_patch(patch.object(rv4,"latest_v4",side_effect=lambda:copy.deepcopy(self.study)))
        self.heavy = self.add_patch(patch.object(rv4,"run_study",side_effect=AssertionError("Real research forbidden in API tests")))
        self.run_suite = self.add_patch(patch.object(api,"run_suite",return_value=({"run_id":"mock_run","status":"complete"},None)))
        self.snapshot = self.add_patch(patch.object(api,"make_snapshot"))
        self.build = self.add_patch(patch.object(api,"build_dashboard",return_value=None))
        self.ordinary = self.add_patch(patch.object(api,"latest",return_value=None))
        self.process = self.add_patch(patch.object(api.subprocess,"run",return_value=SimpleNamespace(returncode=0)))
        self.factory = self.add_patch(patch.object(api,"JOB_THREAD",side_effect=self.fake_thread))

    def add_patch(self, patcher):
        self.patchers.append(patcher)
        return patcher.start()

    def tearDown(self):
        self.heavy.assert_not_called()
        for p in reversed(self.patchers):
            p.stop()
        api.LOCAL_JOBS.clear()
        api.LOCAL_JOBS.update(self.saved_jobs)
        fixture=self.root.resolve()
        fixture.relative_to(Path(__file__).resolve().parent)
        if fixture.name.startswith("_v4_api_fixture_"):
            shutil.rmtree(fixture)

    def fake_thread(self, **kwargs):
        return SimpleNamespace(start=lambda:self.jobs.append(kwargs))

    def valid_study(self):
        c = next(x for x in rv4.grid() if x["factor_id"]=="lowvol_w020" and x["top_n"]==20
                 and x["universe"]=="all_liquid" and x["rebalance"]=="monthly")
        c["checks"] = {"numeric_pass":True}
        periods = copy.deepcopy(research.PERIODS)
        periods["evaluation_2025"] = ["2025-02-03","2025-11-28"]
        return {"study_id":"mock_reviewed_study","status":"complete","snapshot_ready":True,
                "validation":{"passed":True},"candidates":[c],
                "protocol":{"pilot":False,"code_sha256":self.hashes,"source_identity":self.source_identity,"periods":periods,
                            "universes":copy.deepcopy(research.UNIVERSES),
                            "base_config":asdict(research.base_config(capital=20_000_000,wind_down_days=40,max_participation=.007))}}

    def request(self, route, value=None, raw=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1",self.httpd.server_port,timeout=5)
        headers = {"Content-Type":"application/json"}
        if extra_headers: headers.update(extra_headers)
        data = raw if raw is not None else (json.dumps(value).encode() if value is not None else None)
        connection.request("POST" if data is not None else "GET",route,body=data,headers=headers)
        response = connection.getresponse()
        code, body = response.status, json.loads(response.read())
        connection.close()
        return code, body

    def start_v4(self):
        code, accepted = self.request("/api/research/v4/run",{"confirmed_full_study":True,"workers":2})
        self.assertEqual(code,202)
        return accepted

    def run_fake_job(self):
        job = self.jobs.pop(0)
        job["target"](*job.get("args",()))

    def set_completed_progress(self):
        started = (datetime.fromisoformat(api.STATE["started_at"])+timedelta(seconds=1)).isoformat()
        self.progress.return_value = {"job_id":self.study["study_id"],"status":"complete","phase":"complete",
                                      "started_at":started,"updated_at":started,"snapshot_ready":True,"completed":5616,"total":5616}

    def test_flat_run_has_persisted_identity_busy_conflict_and_same_completion_id(self):
        code, accepted = self.request("/api/run",{"capital":100000})
        self.assertEqual(code,202); self.assertEqual(accepted["job_kind"],"run")
        code, conflict = self.request("/api/run",{})
        self.assertEqual(code,409); self.assertEqual(conflict["job_id"],accepted["job_id"])
        self.assertEqual(self.request("/api/status")[1]["job_id"],accepted["job_id"])
        self.run_fake_job()
        status = self.request("/api/status")[1]
        self.assertFalse(status["busy"]); self.assertEqual(status["status"],"complete")
        self.assertEqual(status["job_id"],accepted["job_id"])
        persisted = json.loads(api.JOB_FILE.read_text())
        self.assertEqual(persisted["job_id"],accepted["job_id"])
        self.assertEqual(persisted["status"],"complete")
        self.run_suite.assert_called_once()

    def test_unknown_nested_fields_numbers_and_legacy_exit_rejected_before_thread(self):
        bad = [{"config":{},"unexpected":1}, {"config":{"unknown":1}}, {"config":{"top_n":True}},
               {"config":{"execution_model":"legacy","wind_down_days":20}},
               {"config":{"execution_model":"legacy","liquidate_end":True}},
               {"config":{"st_policy":"strict"}}, {"config":[]}, {"wind_down_days":121}]
        for body in bad:
            with self.subTest(body=body): self.assertEqual(self.request("/api/run",body)[0],400)
        self.assertEqual(self.request("/api/run",raw=b'{"capital":NaN}')[0],400)
        self.assertEqual(self.request("/api/research/v4/run",{"confirmed_full_study":True,"workers":True})[0],400)
        self.assertEqual(self.request("/api/research/v4/run",{"confirmed_full_study":True,"retry_failed":1})[0],400)
        self.factory.assert_not_called(); self.run_suite.assert_not_called()

    def test_old_progress_cannot_complete_queued_new_api_job(self):
        self.progress.return_value = {"job_id":"old_study","started_at":"2020-01-01T00:00:00+08:00",
                                      "status":"complete","completed":5616,"snapshot_ready":True}
        accepted = self.start_v4()
        progress = self.request("/api/research/v4/progress")[1]
        self.assertEqual(progress["status"],"queued"); self.assertEqual(progress["completed"],0)
        self.assertEqual(progress["job_id"],accepted["job_id"])
        self.assertIsNone(progress["study_id"])
        self.assertTrue(self.request("/api/status")[1]["busy"])

    def test_matching_api_progress_binds_study_and_keeps_accepted_job_id(self):
        accepted = self.start_v4(); self.set_completed_progress()
        self.progress.return_value["status"] = "running"
        p = self.request("/api/research/v4/progress")[1]
        self.assertEqual(p["job_id"],accepted["job_id"])
        self.assertEqual(p["study_id"],self.study["study_id"])
        self.assertEqual(api.STATE["study_id"],self.study["study_id"])

    def test_child_completion_does_not_finish_api_job_before_snapshot_stage(self):
        accepted=self.start_v4(); self.set_completed_progress()
        p=self.request("/api/research/v4/progress")[1]
        self.assertEqual(p["job_id"],accepted["job_id"])
        self.assertEqual(p["status"],"running")
        self.assertIn("核对验收",p["phase"])
        self.assertTrue(self.request("/api/status")[1]["busy"])

    def test_cli_lock_uses_cli_identity_and_does_not_complete_old_api_identity(self):
        api.STATE.update(busy=True,job_kind="research_v4",job_id="old_api",study_id="old_study",
                         status="running",started_at="2020-01-01T00:00:00+08:00")
        self.active.return_value = True
        self.progress.return_value = {"job_id":"new_cli","status":"running","phase":"selection_2024",
                                      "started_at":"2026-10-17T00:20:56+08:00"}
        self.assertEqual(self.request("/api/status")[1]["job_id"],"new_cli")
        self.assertEqual(self.request("/api/research/v4/progress")[1]["job_id"],"new_cli")
        code, conflict = self.request("/api/run",{})
        self.assertEqual(code,409); self.assertEqual(conflict["job_id"],"new_cli")
        self.active.return_value = False; self.progress.return_value["status"]="complete"
        self.assertEqual(self.request("/api/status")[1]["status"],"interrupted")
        self.assertFalse(api.STATE["busy"])
        self.assertEqual(self.request("/api/research/v4/progress")[1]["job_id"],"new_cli")
        self.assertEqual(self.request("/api/run",{})[0],202)

    def test_restart_reconciles_bound_completed_job_and_releases_stale_busy(self):
        state = {"busy":True,"job_kind":"research_v4","job_id":"api_bound","study_id":"study_bound",
                 "status":"running","started_at":"2026-10-17T00:00:00+08:00"}
        api.JOB_FILE.write_text(json.dumps(state))
        self.progress.return_value = {"job_id":"study_bound","status":"complete","phase":"complete",
                                      "started_at":"2026-10-17T00:01:00+08:00"}
        api._restore_state()
        self.assertFalse(api.STATE["busy"]); self.assertEqual(api.STATE["job_id"],"api_bound")
        self.assertEqual(api.STATE["status"],"complete")
        self.assertEqual(self.request("/api/run",{})[0],202)

    def test_restart_with_unbound_old_job_never_claims_new_cli_completion(self):
        api.JOB_FILE.write_text(json.dumps({"busy":True,"job_kind":"research_v4","job_id":"unbound_old",
                                            "status":"running","started_at":"2020-01-01T00:00:00+08:00"}))
        self.progress.return_value = {"job_id":"different_study","status":"complete",
                                      "started_at":"2026-10-17T00:01:00+08:00"}
        api._restore_state()
        self.assertFalse(api.STATE["busy"]); self.assertEqual(api.STATE["status"],"interrupted")

    def test_active_cli_after_restart_is_still_busy(self):
        api.JOB_FILE.write_text(json.dumps({"busy":True,"job_kind":"research_v4","job_id":"api_before_restart",
                                            "study_id":"cli_running","status":"running"}))
        self.active.return_value=True
        self.progress.return_value={"job_id":"cli_running","status":"running","phase":"evaluation_2025",
                                    "started_at":"2026-10-17T00:01:00+08:00"}
        api._restore_state()
        self.assertTrue(self.request("/api/status")[1]["busy"])
        self.assertEqual(self.request("/api/run",{})[0],409)

    def test_v4_activation_reads_once_and_uses_frozen_protocol_configuration(self):
        c = self.study["candidates"][0]
        code, accepted = self.request("/api/research/v4/activate",{"experiment_id":c["experiment_id"]})
        self.assertEqual(code,202); self.latest.assert_called_once()
        self.assertEqual(accepted["source_study_id"],self.study["study_id"])
        for key, value in {"capital":20_000_000,"wind_down_days":40,"max_participation":.007,
                           "start":"2025-02-03","end":"2025-11-28","top_n":20,"rebalance":"monthly"}.items():
            self.assertEqual(accepted["config"][key],value)
        self.assertEqual(self.jobs[0]["args"][1]["score"]["op"],"v4_lowvol_w020")
        self.run_fake_job(); self.run_suite.assert_called_once()

    def test_unverified_pilot_version_mismatch_or_invalid_candidate_cannot_activate(self):
        baseline = copy.deepcopy(self.study)
        changes = [("snapshot",lambda x:x.update(snapshot_ready=False)),
                   ("validation",lambda x:x.update(validation={"passed":False})),
                   ("pilot",lambda x:x["protocol"].update(pilot=True)),
                   ("version",lambda x:x["protocol"].update(code_sha256={})),
                   ("candidate",lambda x:x["candidates"][0].update(checks={"numeric_pass":False}))]
        for name, change in changes:
            self.study=copy.deepcopy(baseline); change(self.study)
            with self.subTest(name=name):
                self.assertEqual(self.request("/api/research/v4/activate",{"experiment_id":baseline["candidates"][0]["experiment_id"]})[0],400)
        self.factory.assert_not_called(); self.snapshot.assert_not_called()

    def test_source_match_is_checked_at_activation_but_never_polled(self):
        self.request("/api/status")
        self.request("/api/research/v4/progress")
        self.request("/api/research/v4")
        self.source.assert_not_called()
        c=self.study["candidates"][0]
        self.assertEqual(self.request("/api/research/v4/activate",{"experiment_id":c["experiment_id"]})[0],202)
        self.source.assert_called_once_with()

    def test_source_mismatch_rejects_activation_before_thread(self):
        self.source.return_value={**self.source_identity,"size_bytes":1,"sha256":"changed-source"}
        c=self.study["candidates"][0]
        code,body=self.request("/api/research/v4/activate",{"experiment_id":c["experiment_id"]})
        self.assertEqual(code,400); self.assertIn("源身份不同",body["error"])
        self.factory.assert_not_called(); self.run_suite.assert_not_called()

    def test_source_helper_rejects_environment_override_without_reading_zip(self):
        self.source.side_effect=ValueError("V4仅允许用户指定原ZIP；拒绝环境变量替换源")
        c=self.study["candidates"][0]
        code,body=self.request("/api/research/v4/activate",{"experiment_id":c["experiment_id"]})
        self.assertEqual(code,400); self.assertIn("环境变量",body["error"])
        self.factory.assert_not_called(); self.snapshot.assert_not_called()

    def test_v4_unverified_completion_never_builds_snapshot(self):
        accepted=self.start_v4(); self.set_completed_progress(); self.study["validation"]["passed"]=False
        self.run_fake_job()
        self.snapshot.assert_not_called(); self.build.assert_not_called()
        self.assertEqual(api.STATE["status"],"failed"); self.assertFalse(api.STATE["busy"])
        self.assertEqual(api.STATE["job_id"],accepted["job_id"])

    def test_v4_verified_completion_without_ordinary_run_does_not_claim_snapshot(self):
        self.start_v4(); self.set_completed_progress(); self.run_fake_job()
        self.assertEqual(api.STATE["status"],"complete"); self.assertFalse(api.STATE["snapshot_built"])
        self.assertIn("未生成",api.STATE["progress"])
        self.snapshot.assert_not_called(); self.build.assert_not_called()

    def test_v4_verified_completion_builds_before_same_job_becomes_complete(self):
        accepted=self.start_v4(); self.set_completed_progress()
        self.ordinary.return_value={"run_id":"ordinary_main","status":"complete"}
        def building():
            self.assertTrue(api.STATE["busy"]); self.assertEqual(api.STATE["status"],"building")
            return None
        self.build.side_effect=building
        self.run_fake_job()
        self.assertEqual(api.STATE["job_id"],accepted["job_id"])
        self.assertEqual(api.STATE["status"],"complete"); self.assertTrue(api.STATE["snapshot_built"])
        self.snapshot.assert_called_once(); self.build.assert_called_once()

    def test_failed_job_thread_start_does_not_leave_busy(self):
        self.factory.side_effect=RuntimeError("mock thread unavailable")
        code, body=self.request("/api/run",{})
        self.assertEqual(code,500); self.assertFalse(api.STATE["busy"])
        self.assertEqual(api.STATE["status"],"failed"); self.assertEqual(body["job_id"],api.STATE["job_id"])

    def test_pack_concurrent_same_id_is_atomic_and_single_entry(self):
        pack={"format":"daily-factor-block-pack","version":1,"id":"api_pack","name":"Mock pack",
              "blocks":[{"id":"custom.api_echo","name":"Echo","inputs":[],
                         "template":{"op":"close","args":[],"params":{}}}]}
        original_save=research.save_json; counter={"active":0,"max":0}; guard=threading.Lock()
        def watched_save(path,value):
            with guard:
                counter["active"]+=1;counter["max"]=max(counter["max"],counter["active"])
            try:
                time.sleep(.03)
                original_save(path,value)
            finally:
                with guard:counter["active"]-=1
        with patch.object(research,"save_json",side_effect=watched_save):
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures=[workers.submit(self.request,"/api/block-packs",{"pack":copy.deepcopy(pack)}) for _ in range(2)]
                replies=[f.result(timeout=5) for f in futures]
        self.assertTrue(all(code==201 for code,_ in replies)); self.assertEqual(replies[0][1]["id"],replies[1][1]["id"])
        self.assertEqual(counter["max"],1); self.assertEqual(len(list(api.PACKS.glob("*.json"))),1)
        self.assertEqual(len(api.read_documents(api.PACKS)),1)

    def test_origin_unknown_route_and_wrong_json_type_rejected(self):
        self.assertEqual(self.request("/api/run",{},extra_headers={"Origin":"https://foreign.example"})[0],403)
        self.assertEqual(self.request("/api/not-a-route",{})[0],404)
        self.assertEqual(self.request("/api/run",raw=b'[]')[0],400)
        self.assertEqual(self.request("/api/run",{},extra_headers={"Content-Type":"text/plain"})[0],415)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(V4HTTPTests))
    report={"scope":"isolated loopback HTTP with mocked compute, subprocess and job threads",
            "tests_run":result.testsRun,"passed":result.wasSuccessful(),
            "failures":[str(t) for t,_ in result.failures],"errors":[str(t) for t,_ in result.errors],
            "live_server_contacted":False,"live_progress_or_queue_read":False,"real_Market_loaded":False,
            "real_research_or_V3_run_started":False,"frozen_compute_files_modified":False,
            "resource":V4HTTPTests.resource,"numeric_threads":1,"arrow_threads":1}
    target=Path(__file__).resolve().parents[1]/"metadata/V4_API_AUDIT.json"
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="resource"},ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
