"""Small independent formula, causality, adjustment and missing-data checks."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v4_factors as vf


def market(n=190, k=5):
    rng = np.random.default_rng(9102026)
    c = 15 * np.exp(np.cumsum(rng.normal(.0003, .012, (n, k)), axis=0))
    o = c * np.exp(rng.normal(0, .004, (n, k)))
    raw = {"open": o, "close": c, "high": np.maximum(o, c)*1.015,
           "low": np.minimum(o, c)*.985, "cumadj": np.ones_like(c),
           "amt": rng.uniform(2e7, 3e8, (n, k)),
           "num_trades": rng.integers(1000, 20000, (n, k)).astype(float)}
    return SimpleNamespace(raw=raw)


class FactorTests(unittest.TestCase):
    def test_catalog_and_candidate_count_have_no_reverse_duplicates(self):
        cat = vf.catalog()
        self.assertEqual(len(cat), 78)
        self.assertEqual(len({s["idea_id"] for s in cat}), 26)
        self.assertEqual(len(vf.candidate_grid()), 1404)
        signatures = [(s["raw_formula"], s["window"]) for s in cat]
        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertEqual(len({x["experiment_id"] for x in vf.candidate_grid()}), 1404)
        cat[0]["boundaries"].clear()
        self.assertTrue(vf.catalog()[0]["boundaries"])
        for s in cat:
            self.assertGreaterEqual(s["min_history"], s["window"])
            self.assertIn(s["direction"], (-1, 1))

    def test_complete_fixture_every_spec_is_usable_and_warmup_is_enforced(self):
        m = market()
        engine = vf.FactorEngine(m)
        for s in vf.catalog():
            with self.subTest(factor=s["id"]):
                x = engine.compute(s["id"])
                self.assertEqual(x.shape, m.raw["close"].shape)
                self.assertEqual(x.dtype, np.float64)
                self.assertTrue(np.isnan(x[:s["min_history"]-1]).all())
                self.assertTrue(np.isfinite(x[s["min_history"]-1:]).all())
                self.assertFalse(np.isinf(x).any())

    def test_independent_hand_formula_baseline_and_distinct_mechanisms(self):
        m = market(); e = vf.FactorEngine(m)
        c, o, h, l, a, n = [m.raw[k][:, 0] for k in ("close", "open", "high", "low", "amt", "num_trades")]
        t = 150; w = 20; ix = slice(t-w+1, t+1)
        r = c[1:]/c[:-1]-1; rw = r[t-w:t]
        expected = {
            "reversal_w005": -(c[t]/c[t-5]-1),
            "momentum_skip5_w060": c[t-5]/c[t-65]-1,
            "lowvol_w020": -np.std(rw, ddof=1),
            "lowrange_w020": -np.mean((h[ix]-l[ix])/c[ix]),
            "liquidity_w020": np.log1p(np.mean(a[ix])),
            "downside_risk_w020": -np.sqrt(np.mean(np.minimum(rw, 0)**2)),
            "tail_return_w020": np.quantile(rw, .1),
            "positive_fraction_w020": np.mean(rw > 0),
            "closing_position_w020": np.mean((2*c[ix]-h[ix]-l[ix])/(h[ix]-l[ix])),
            "channel_position_w020": (c[t]-np.min(l[ix]))/(np.max(h[ix])-np.min(l[ix])),
            "amihud_w020": -np.mean(abs(rw)/(a[ix]/1e8)),
            "return_amount_corr_w020": -np.corrcoef(rw, np.log1p(a[ix]))[0, 1],
            "average_trade_value_w020": np.log1p(np.mean(a[ix]/n[ix])),
            "overnight_reversal_w020": -np.mean(o[ix]/c[t-w:t]-1),
        }
        for fid, value in expected.items():
            with self.subTest(factor=fid):
                self.assertAlmostEqual(float(e.compute(fid)[t, 0]), float(value), places=10)

    def test_prefix_and_future_perturbation_all_specs(self):
        m = market(); cutoff = 135
        changed = copy.deepcopy(m)
        for key in vf.SOURCE_FIELDS:
            changed.raw[key][cutoff+1:] *= 3.7
        prefix = SimpleNamespace(raw={k:v[:cutoff+1].copy() for k,v in m.raw.items()})
        full_e, changed_e, short_e = [vf.FactorEngine(x) for x in (m, changed, prefix)]
        for fid in vf.FACTOR_SPECS:
            with self.subTest(factor=fid):
                x = full_e.compute(fid)[:cutoff+1]
                np.testing.assert_allclose(x, changed_e.compute(fid)[:cutoff+1], atol=1e-11, rtol=1e-11, equal_nan=True)
                np.testing.assert_allclose(x, short_e.compute(fid), atol=1e-11, rtol=1e-11, equal_nan=True)

    def test_consistent_split_adjustment_does_not_create_factor_signal(self):
        m = market(); split = copy.deepcopy(m)
        for key in ("open", "close", "high", "low"):
            split.raw[key][80:] /= 2
        split.raw["cumadj"][80:] *= 2
        before, after = vf.FactorEngine(m), vf.FactorEngine(split)
        for fid in vf.FACTOR_SPECS:
            with self.subTest(factor=fid):
                np.testing.assert_allclose(before.compute(fid), after.compute(fid), atol=1e-11, rtol=1e-11, equal_nan=True)

    def test_missing_interior_observation_invalidates_entire_history_path(self):
        m = market(); m.raw["close"][100, 0] = np.nan
        e = vf.FactorEngine(m)
        for fid in ("reversal_w005", "momentum_w020", "lowvol_w020", "lowrange_w020"):
            x = e.compute(fid)
            self.assertTrue(np.isnan(x[100, 0]))
        self.assertTrue(np.isnan(e.compute("momentum_w020")[120, 0]))
        self.assertTrue(np.isfinite(e.compute("momentum_w020")[121, 0]))
        self.assertTrue(np.isnan(e.compute("lowvol_w020")[120, 0]))
        self.assertTrue(np.isfinite(e.compute("lowvol_w020")[121, 0]))
        self.assertTrue(np.isnan(e.compute("lowrange_w020")[119, 0]))
        self.assertTrue(np.isfinite(e.compute("lowrange_w020")[120, 0]))

    def test_zero_negative_missing_and_flat_denominator_are_not_neutral_scores(self):
        m = market(); m.raw["amt"][100, 0] = -1; m.raw["num_trades"][100, 1] = 0
        m.raw["cumadj"][100, 2] = 0
        m.raw["high"][100, 3] = m.raw["low"][100, 3] = m.raw["close"][100, 3]
        m.raw["close"][:, 4] = 10
        e = vf.FactorEngine(m)
        self.assertTrue(np.isnan(e.compute("liquidity_w020")[110, 0]))
        self.assertTrue(np.isnan(e.compute("average_trade_value_w020")[110, 1]))
        self.assertTrue(np.isnan(e.compute("momentum_w020")[110, 2]))
        self.assertTrue(np.isnan(e.compute("closing_position_w020")[110, 3]))
        self.assertTrue(np.isnan(e.compute("return_to_risk_w020")[150, 4]))
        self.assertTrue(np.isnan(e.compute("return_amount_corr_w020")[150, 4]))
        self.assertEqual(e.compute("lowvol_w020")[150, 4], 0)
        # Zero actual turnover is distinct from a missing or negative amount.
        m.raw["amt"][:, 0] = 0; e.clear()
        self.assertEqual(e.compute("liquidity_w020")[150, 0], 0)
        self.assertTrue(np.isnan(e.compute("amihud_w020")[150, 0]))

    def test_rank_uses_only_final_universe_and_handles_ties_and_too_few(self):
        m = market(); m.raw["amt"][:] = [2, 2, 3, 4, 5]
        e = vf.FactorEngine(m)
        mask = np.broadcast_to([True, True, False, True, False], e.shape)
        ranked = e.score("liquidity_w020", mask)
        np.testing.assert_allclose(ranked[150], [.5, .5, np.nan, 1, np.nan], equal_nan=True)
        one = np.broadcast_to([False, False, True, False, False], e.shape)
        self.assertTrue(np.isnan(e.score("liquidity_w020", one)).all())
        self.assertTrue(np.isnan(e.compute("liquidity_w020", mask)[150, 2]))

    def test_cache_is_bounded_immutable_and_never_mutates_source(self):
        m = market(); original = {k:v.copy() for k,v in m.raw.items()}
        e = vf.FactorEngine(m, max_cache=2)
        for fid in vf.FACTOR_SPECS:
            x = e.compute(fid)
            self.assertLessEqual(len(e._cache), 2)
            self.assertFalse(x.flags.writeable)
        e.clear(); self.assertEqual(len(e._cache), 0); self.assertEqual(len(e._primitives), 0)
        for k in original:
            np.testing.assert_array_equal(original[k], m.raw[k])

    def test_baseline_equivalence_to_existing_engine_on_valid_fixture(self):
        import lab
        import research
        m = market()
        old = lab.Market.__new__(lab.Market)
        old.raw = m.raw; old.symbols = np.array([f"{j:06d}.SZ" for j in range(5)])
        old.dates = pd.bdate_range("2023-01-02", periods=190)
        old.prepare()
        existing = research.factor_matrices(old); e = vf.FactorEngine(old)
        for legacy, fid in vf.BASELINE_EQUIVALENTS.items():
            with self.subTest(legacy=legacy):
                np.testing.assert_allclose(e.compute(fid), existing[legacy], atol=1e-11, rtol=1e-11, equal_nan=True)

    def test_invalid_shapes_ids_and_grid_are_rejected(self):
        m = market(); del m.raw["num_trades"]
        with self.assertRaises(ValueError): vf.FactorEngine(m)
        with self.assertRaises(ValueError): vf.FactorEngine(market(), max_cache=3)
        with self.assertRaises(KeyError): vf.FactorEngine(market()).compute("missing")
        with self.assertRaises(ValueError): vf.candidate_grid(factor_ids=["missing"])
        with self.assertRaises(ValueError): vf.candidate_grid(top_ns=(20, 20))
        with self.assertRaises(ValueError): vf.FactorEngine(market()).compute("lowvol_w020", np.ones((1,1), bool))


if __name__ == "__main__":
    unittest.main(verbosity=2)
