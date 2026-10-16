"""Pure mock neighborhood tests; no research files or live queues are read."""
import copy
import csv
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v4_diagnostics as diagnostic


def candidate(identity, idea="reversal", window=5, pool="all_liquid", count=50, freq="weekly", ret=.1):
    rows = {field: {"status": "success", "total_return": ret, "sharpe": 1.} for field in diagnostic.SCENARIOS.values()}
    return {"experiment_id": identity, "idea_id": idea, "factor_id": f"{idea}_{window}",
            "window": window, "universe": pool, "top_n": count, "rebalance": freq,
            "readiness": "original readiness", **rows}


def records(report):
    return {r["experiment_id"]: r for r in report["neighbors"]}


class NeighborhoodTests(unittest.TestCase):
    def test_exactly_one_axis_differs_same_family_any_window_level(self):
        data = [candidate("base"), candidate("window20", window=20), candidate("window60", window=60),
                candidate("pool", pool="liquid1000"), candidate("count", count=100), candidate("freq", freq="monthly"),
                candidate("two_changes", window=20, count=100), candidate("other_family", idea="momentum"),
                candidate("same_axes_different_id")]
        base = records(diagnostic.diagnose({"candidates": data}))["base"]
        self.assertEqual(set(base["neighbor_ids"]), {"window20", "window60", "pool", "count", "freq"})
        self.assertEqual(base["neighbor_changed_axes"]["window60"], "window")
        self.assertEqual(base["neighbor_count"], 5)

    def test_failed_missing_and_nonfinite_are_separate_and_never_zero_filled(self):
        base = candidate("base")
        good = candidate("good", window=20, ret=.2)
        failed = candidate("failed", pool="liquid1000", ret=999.)
        failed["evaluation_2025"] = {"status": "failed", "total_return": 999.}
        absent = candidate("absent", count=100);del absent["evaluation_2025"]
        nonfinite = candidate("nan", freq="monthly");nonfinite["evaluation_2025"]["total_return"] = float("nan")
        result = records(diagnostic.diagnose({"candidates": [base, good, failed, absent, nonfinite]}))["base"]
        self.assertEqual((result["evaluation_expected"], result["evaluation_observed"], result["evaluation_failed"], result["evaluation_unknown"]), (4, 1, 1, 2))
        self.assertEqual(result["evaluation_positive_fraction"], 1.)
        self.assertEqual(result["evaluation_median"], .2)
        self.assertEqual(result["evaluation_worst"], .2)
        self.assertEqual(result["evaluation"], {"expected": 4, "observed": 1, "failed": 1, "unknown": 2,
                                                "positive_fraction": 1., "return_median": .2, "return_worst": .2, "return_best": .2})

    def test_positive_isolation_requires_positive_self_and_nonpositive_neighbors(self):
        base = candidate("base", ret=.5)
        negative = candidate("negative", window=20, ret=-.1)
        zero = candidate("zero", count=100, ret=0.)
        result = records(diagnostic.diagnose({"candidates": [base, negative, zero]}))["base"]
        self.assertTrue(result["isolated_positive_peak"])
        self.assertEqual(result["evaluation_positive_fraction"], 0.)
        self.assertEqual(result["own_minus_neighbor_median"], .55)
        self.assertEqual(result["isolation_status"], "isolated_positive_peak")

    def test_incomplete_neighbors_cannot_confirm_complete_isolated_peak(self):
        base, negative, missing = candidate("base", ret=.5), candidate("negative", window=20, ret=-.1), candidate("missing", count=100)
        missing["evaluation_2025"] = {"status": "failed"}
        result = records(diagnostic.diagnose({"candidates": [base, negative, missing]}))["base"]
        self.assertTrue(result["observed_positive_isolation"])
        self.assertIsNone(result["isolated_positive_peak"])
        self.assertEqual(result["isolation_status"], "observed_isolation_incomplete_coverage")

    def test_no_family_neighbor_or_no_observed_metric_means_null_not_zero(self):
        only = records(diagnostic.diagnose({"candidates": [candidate("only")]}))["only"]
        self.assertEqual(only["neighbor_count"], 0)
        self.assertIsNone(only["evaluation_positive_fraction"])
        self.assertIsNone(only["evaluation_median"])
        self.assertIsNone(only["isolated_positive_peak"])
        failed = candidate("failed", window=20);failed["evaluation_2025"] = {"status": "failed"}
        result = records(diagnostic.diagnose({"candidates": [candidate("base"), failed]}))["base"]
        self.assertEqual(result["isolation_status"], "neighbors_unavailable")
        self.assertEqual(result["evaluation_failed"], 1)

    def test_metric_denominators_are_independent_and_input_is_unchanged(self):
        data = [candidate("base"), candidate("neighbor", window=20)]
        data[1]["selection_2024"]["sharpe"] = None
        data[1]["stress_2025"] = {"status": "failed"}
        data[1]["capacity_2025"]["total_return"] = -.1
        preserved = copy.deepcopy(data)
        result = records(diagnostic.diagnose({"candidates": data}))["base"]
        self.assertEqual(result["training_observed"], 1)
        self.assertEqual(result["training_sharpe_observed"], 0)
        self.assertEqual(result["training_sharpe_unknown"], 1)
        self.assertEqual(result["stress_failed"], 1)
        self.assertIsNone(result["stress_positive_fraction"])
        self.assertEqual(result["capacity_positive_fraction"], 0.)
        self.assertEqual(data, preserved)

    def test_family_variants_are_counts_not_independent_evidence_or_new_readiness(self):
        data = [candidate("w5"), candidate("w20", window=20), candidate("w60", window=60)]
        data[0]["readiness"] = diagnostic.PASS_LABEL
        data[1]["readiness"] = "含非成交减记"
        data[1]["capacity_2025"]["status"] = "failed"
        report = diagnostic.diagnose({"candidates": data})
        family = report["families"][0]
        self.assertEqual((family["configuration_count"], family["window_spec_count"], family["all_four_status_success_count"], family["existing_research_pass_count"]), (3, 3, 2, 1))
        self.assertTrue(family["variants_are_not_independent_evidence"])
        self.assertFalse(report["changes_selection_or_readiness"])
        self.assertEqual(report["additional_simulations"], 0)
        self.assertEqual(data[1]["readiness"], "含非成交减记")

    def test_missing_axis_never_creates_fake_neighbor(self):
        base, incomplete = candidate("base"), candidate("incomplete", window=20)
        del incomplete["top_n"]
        report = diagnostic.diagnose({"candidates": [base, incomplete]})
        self.assertEqual(records(report)["base"]["neighbor_count"], 0)
        self.assertFalse(records(report)["incomplete"]["neighborhood_definition_complete"])
        self.assertTrue(report["warnings"])

    def test_duplicate_ids_are_rejected_instead_of_double_counting(self):
        with self.assertRaises(ValueError):
            diagnostic.diagnose({"candidates": [candidate("same"), candidate("same", window=20)]})

    def test_explicit_export_writes_only_new_output_and_preserves_nulls(self):
        output = Path(__file__).resolve().parent / ("neighbors_fixture_" + uuid.uuid4().hex)
        def cleanup():
            resolved = output.resolve();resolved.relative_to(Path(__file__).resolve().parent)
            if resolved.exists():shutil.rmtree(resolved)
        self.addCleanup(cleanup)
        report = diagnostic.diagnose({"study_id": "mock-only", "snapshot_revision": "frozen-mock-revision", "candidates": [candidate("only")]}, output)
        self.assertEqual(set(p.name for p in output.iterdir()), {"neighbor_diagnostics.csv", "family_summary.csv", "diagnostics.json", "report.md"})
        stored = json.loads((output / "diagnostics.json").read_text(encoding="utf-8"))
        self.assertIsNone(stored["neighbors"][0]["evaluation_positive_fraction"])
        with (output / "neighbor_diagnostics.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(next(csv.DictReader(stream))["evaluation_positive_fraction"], "")
        self.assertEqual(report["study_id"], "mock-only")
        self.assertEqual(report["source_snapshot_revision"], "frozen-mock-revision")
        self.assertEqual(len(report["script_sha256"]), 64)
        self.assertEqual(report["family_summary"], report["families"])
        self.assertIsNone(report["neighbors"][0]["training"]["sharpe_median"])
        self.assertIn("generated_at", report)


if __name__ == "__main__":unittest.main()
