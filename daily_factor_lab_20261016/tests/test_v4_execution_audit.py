"""V4 regressions based on hand accounting and deliberate counterfactuals.

Only this process is changed: one numeric thread, two performance cores
reserved. No source archive is opened and no previous research is rerun.
"""
import os
for _key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_key] = "1"

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab
import execution_metadata as metadata
import research
from test_execution_precision import market, config, run

pa.set_cpu_count(1)
pa.set_io_thread_count(1)
RESOURCE_POLICY = research.reserve_performance_cores()


class V4ExecutionAuditTests(unittest.TestCase):
    def test_negative_nonfinite_and_boolean_fee_multipliers_are_rejected(self):
        m = market()
        for multiplier in (-1., float("nan"), float("inf"), -float("inf"), True, np.bool_(False)):
            with self.subTest(multiplier=str(multiplier)), self.assertRaisesRegex(ValueError, "cost_multiplier"):
                lab.simulate(m, np.ones_like(m.close), config(m), cost_multiplier=multiplier)
        gross = lab.simulate(m, np.ones_like(m.close), config(m, commission=.0001, slippage=.0005), cost_multiplier=0.)
        np.testing.assert_allclose(gross["daily"].nav, 1e6)
        np.testing.assert_allclose(gross["daily"].cost, 0.)

    def test_direct_invalid_config_cannot_bypass_public_simulation_validation(self):
        m = market()
        invalid = lab.Config(start=str(m.dates[65].date()), end=str(m.dates[-1].date()), commission=-.001)
        with self.assertRaises(ValueError):
            lab.simulate(m, np.ones_like(m.close), invalid)

    def test_pending_buy_rechecks_finite_filter_and_keeps_old_target(self):
        m = market(symbols=("000001.SZ", "000002.SZ"))
        m.raw["amt"][65, 0] = 100000.  # First partial fill=10,000, target=950,000.
        m.prepare()
        scores = np.tile([2., 1.], (len(m.dates), 1))
        scores[65, 0] = np.nan  # Compiler filter invalid at close65, before retry66.
        scores[66:69, :] = [0., 100.]  # Higher new ranking must not replace target.
        out = run(m, scores=scores)
        self.assertEqual(out["daily"].trades.iloc[0], 1)
        self.assertEqual(out["daily"].trades.iloc[1], 0)
        rejected = out["orders"].loc[(out["orders"].date == str(m.dates[66].date())) & (out["orders"].side == "BUY")]
        self.assertTrue(rejected.reason.eq("no_longer_finite_score_or_filter").all())
        later = out["trades"].loc[out["trades"].date == str(m.dates[67].date())]
        self.assertTrue(later.symbol.eq("000001.SZ").all())
        self.assertGreater(len(later), 0)
        self.assertEqual(float(later.raw_share_equivalent.iloc[0]), 94000.)

    def test_factor_recovery_does_not_silently_skip_unknown_action_reference(self):
        m = market()
        m.raw["cumadj"][70, 0] = np.nan
        m.raw["cumadj"][71:, 0] = 1.02
        m.prepare()
        scores = np.ones_like(m.close)
        scores[69:] = np.nan
        out = run(m, config(m, rebalance="daily"), scores)
        date = str(m.dates[71].date())
        self.assertFalse(out["trades"].date.eq(date).any())
        rejection = out["orders"].loc[out["orders"].date == date]
        self.assertTrue(rejection.reason.eq("unverified_previous_factor_reference").all())
        self.assertGreater(len(rejection), 0)
        sells = out["trades"].loc[out["trades"].side == "SELL"]
        self.assertEqual(sells.iloc[0].date, str(m.dates[72].date()))

    def test_turnover_uses_tick_execution_amount_not_reference_open_amount(self):
        m = market()
        out = run(m, config(m, capital=10000., commission=.0001, min_commission=5., slippage=.0005, cash_buffer=0.))
        trade = out["trades"].iloc[0]
        self.assertAlmostEqual(trade.notional, 9000.)
        self.assertAlmostEqual(trade.execution_notional, 9009.)
        self.assertAlmostEqual(out["daily"].turnover.iloc[0], .9009)
        self.assertAlmostEqual(out["daily"].cash.iloc[0], 985.90991)

    def test_writeoff_zero_positions_are_not_reported_as_trade_exit(self):
        m = market()
        for field in ("open", "high", "low", "close"):
            m.raw[field][70:, 0] = np.nan
        m.raw["amt"][70:, 0] = np.nan
        m.prepare()
        out = run(m, config(m, liquidate_end=True, writeoff_missing_days=3))
        self.assertGreater(out["metrics"]["writeoff_loss"], 0.)
        self.assertTrue(out["terminal"]["fully_liquidated"])  # Backward-compatible zero-book flag.
        self.assertTrue(out["terminal"]["model_position_zero"])
        self.assertFalse(out["terminal"]["fully_exited_by_trades"])
        self.assertEqual(out["terminal"]["writeoff_loss"], out["metrics"]["writeoff_loss"])
        self.assertFalse(out["trades"].side.eq("SELL").any())
        self.assertAlmostEqual(out["daily"].cash.iloc[-1], 49990.5)

    def test_successful_sale_exit_flag_requires_no_writeoff(self):
        m = market()
        out = run(m, config(m, liquidate_end=True, wind_down_days=3))
        self.assertTrue(out["terminal"]["fully_exited_by_trades"])
        self.assertEqual(out["terminal"]["writeoff_loss"], 0.)
        self.assertEqual(out["terminal"]["residual_positions"], 0)

    def test_future_bar_changes_do_not_change_execution_prefix(self):
        baseline, future = market(), market()
        for field in ("open", "high", "low", "close", "cumadj", "amt"):
            future.raw[field][90:] *= 1.2
        future.prepare()
        a, b = run(baseline), run(future)
        pd.testing.assert_frame_equal(a["daily"].iloc[:25], b["daily"].iloc[:25])
        cutoff = str(baseline.dates[89].date())
        pd.testing.assert_frame_equal(a["trades"].loc[a["trades"].date <= cutoff].reset_index(drop=True),
                                      b["trades"].loc[b["trades"].date <= cutoff].reset_index(drop=True))


class MetadataSummaryCacheTests(unittest.TestCase):
    def test_summary_cache_isolated_from_per_simulation_mutation_and_reset(self):
        m = market()
        cfg = config(m)
        rows = np.arange(65, len(m.dates))
        first = metadata.metadata_summary(m, cfg, rows)
        first["board_counts"]["main"] = 123456
        first["max_actual_positions"] = 999
        with patch.object(metadata, "board_labels", side_effect=AssertionError("Expected a cached summary")):
            second = metadata.metadata_summary(m, cfg, rows)
        self.assertEqual(second["board_counts"]["main"], 1)
        self.assertNotIn("max_actual_positions", second)
        m.prepare()
        self.assertEqual(len(m._execution_metadata_cache), 0)
        self.assertEqual(metadata.metadata_summary(m, cfg, rows)["board_counts"]["main"], 1)


if __name__ == "__main__":
    unittest.main()
