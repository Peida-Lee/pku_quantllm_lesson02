"""Aggregate snapshot integration; fixtures only, never open a live study."""
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import v4_snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.base=(v4_snapshot.ROOT/"research_outputs").resolve()
        self.id="v4_snapshot_test_"+uuid.uuid4().hex
        self.directory=self.base/self.id
        self.directory.mkdir()
        (self.directory/"report.md").write_text("PRIMARY ORIGINAL",encoding="utf-8")
        def row(eid,w):
            return {"experiment_id":eid,"idea_id":"risk","family":"risk","window":w,"universe":"x","top_n":50,"rebalance":"monthly",
                    "selection_2024":{"status":"success","total_return":.1,"sharpe":1.},
                    "evaluation_2025":{"status":"success","total_return":.05},
                    "stress_2025":{"status":"success","total_return":.04},
                    "capacity_2025":{"status":"success","total_return":.03},"readiness":"fixture"}
        self.study={"study_id":self.id,"snapshot_revision":"a"*64,"snapshot_ready":True,"validation":{"passed":True},
                    "candidates":[row("a",10),row("b",20)],"files":{"report":self.id+"/report.md"}}
        self.snapshot={"research_v4":self.study,"queries":{}}

    def tearDown(self):
        target=self.directory.resolve()
        target.relative_to(self.base)
        assert target.parent==self.base and target.name.startswith("v4_snapshot_test_")
        shutil.rmtree(target)

    def test_nested_diagnostic_preserves_primary_report_and_provenance(self):
        v4_snapshot.attach_diagnostics(self.snapshot)
        self.assertEqual((self.directory/"report.md").read_text(encoding="utf-8"),"PRIMARY ORIGINAL")
        self.assertTrue((self.directory/"neighbor_diagnostics"/"report.md").is_file())
        q=self.snapshot["queries"]["research_v4_neighbors"]
        self.assertEqual(len(q["rows"]),2)
        self.assertEqual(q["source"]["study_id"],self.id)
        self.assertEqual(q["source"]["source_snapshot_revision"],"a"*64)
        self.assertTrue(self.study["files"]["neighbor_report"].endswith("neighbor_diagnostics/report.md"))

    def test_same_identity_is_reused_and_stale_revision_regenerated(self):
        v4_snapshot.attach_diagnostics(self.snapshot)
        with patch("v4_diagnostics.diagnose",side_effect=AssertionError("No unnecessary regeneration")):
            v4_snapshot.attach_diagnostics(self.snapshot)
        path=self.directory/"neighbor_diagnostics"/"diagnostics.json"
        bad=json.loads(path.read_text(encoding="utf-8"));bad["source_snapshot_revision"]="b"*64
        path.write_text(json.dumps(bad),encoding="utf-8")
        import v4_diagnostics
        original=v4_diagnostics.diagnose
        with patch("v4_diagnostics.diagnose",wraps=original) as diagnose:
            v4_snapshot.attach_diagnostics(self.snapshot)
            diagnose.assert_called_once()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["source_snapshot_revision"],"a"*64)

    def test_unreviewed_study_is_not_published_as_reviewed_diagnostic(self):
        self.study["validation"]["passed"]=False
        v4_snapshot.attach_diagnostics(self.snapshot)
        self.assertEqual(self.snapshot["queries"],{})
        self.assertFalse((self.directory/"neighbor_diagnostics").exists())

    def test_path_escape_is_rejected(self):
        self.study["study_id"]="../../outside"
        with self.assertRaises(ValueError):v4_snapshot.attach_diagnostics(self.snapshot)


if __name__=="__main__":unittest.main()
