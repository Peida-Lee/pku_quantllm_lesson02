"""Synthetic accounting/causality tests for the zip-only conservative model."""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab
import execution_metadata as em


def market(price=10., symbols=("000001.SZ",), n=110):
    m = lab.Market.__new__(lab.Market)
    m.dates = pd.bdate_range("2024-09-02", periods=n)
    m.symbols = np.asarray(symbols)
    shape = (n, len(symbols))
    m.raw = {"open": np.full(shape, price), "close": np.full(shape, price),
             "high": np.full(shape, price * 1.02), "low": np.full(shape, price * .98),
             "amt": np.full(shape, 1e9), "cumadj": np.ones(shape)}
    m.prepare()
    return m


def config(m, **overrides):
    args = dict(start=str(m.dates[65].date()), end=str(m.dates[-1].date()), capital=1e6,
                top_n=1, min_history=65, min_price=2., min_avg_amount=1e7,
                commission=0., min_commission=0., slippage=0., cash_buffer=.05,
                max_participation=.1, max_daily_participation=.1, execution_model="daily_conservative")
    args.update(overrides)
    return lab.Config(**args).validate()


def run(m, cfg=None, scores=None):
    return lab.simulate(m, np.ones_like(m.close) if scores is None else scores, config(m) if cfg is None else cfg)


class ConservativeExecutionTests(unittest.TestCase):
    def test_frozen_order_quantity_ignores_execution_open_gap(self):
        m = market()
        m.raw["open"][65] = 10.10
        m.prepare()
        out = run(m)
        first = out["trades"].iloc[0]
        self.assertAlmostEqual(first.raw_share_equivalent, 95000.)
        self.assertAlmostEqual(first.notional, 959500.)
        self.assertAlmostEqual(out["daily"].cash.iloc[0], 40500. - 959500. * .00001)
        self.assertEqual(first.signal_date, str(m.dates[64].date()))

    def test_tick_slippage_and_cash_accounting_hand_calculation(self):
        m = market()
        cfg = config(m, capital=10000., commission=.0001, min_commission=5., slippage=.0005, cash_buffer=0.)
        out = run(m, cfg)
        first = out["trades"].iloc[0]
        # Buy 900 shares at raw open10, tick fill10.01. Fees=5+9+.09.
        self.assertAlmostEqual(first.raw_share_equivalent, 900.)
        self.assertAlmostEqual(first.fill_price_raw, 10.01)
        self.assertAlmostEqual(first.slippage, 9.)
        self.assertAlmostEqual(first.execution_notional, 9009.)
        self.assertAlmostEqual(first.total, 14.09009)
        self.assertAlmostEqual(first.cash_after, 985.90991)

    def test_true_last_day_liquidation_and_no_terminal_buy(self):
        m = market()
        out = run(m, config(m, liquidate_end=True))
        self.assertTrue(out["terminal"]["fully_liquidated"])
        self.assertEqual(out["terminal"]["residual_positions"], 0)
        # Transfer both sides plus 0.05% sell stamp duty remain mandatory.
        self.assertAlmostEqual(out["daily"].cash.iloc[-1], 999506.)
        last = out["trades"].loc[out["trades"].date == str(m.dates[-1].date())]
        self.assertTrue(last.side.eq("SELL").all())

    def test_terminal_limit_down_leaves_position_instead_of_fake_cash(self):
        m = market()
        for field in ("open", "high", "low", "close"):
            m.raw[field][-1] = 9.
        m.prepare()
        out = run(m, config(m, liquidate_end=True))
        self.assertFalse(out["terminal"]["fully_liquidated"])
        self.assertEqual(out["terminal"]["residual_positions"], 1)
        self.assertGreater(out["terminal"]["residual_value"], 0.)
        self.assertIn("theoretical_lower_limit", out["terminal"]["blocked_reasons"])

    def test_preplanned_three_day_exit_clears_capacity_that_single_day_cannot(self):
        m = market()
        m.raw["amt"][-3:] = 4e6
        m.prepare()
        one_day = run(m, config(m, liquidate_end=True))
        multi_day = run(m, config(m, liquidate_end=True, wind_down_days=3))
        self.assertAlmostEqual(one_day["terminal"]["residual_value"], 550000.)
        self.assertFalse(one_day["terminal"]["fully_liquidated"])
        self.assertTrue(multi_day["terminal"]["fully_liquidated"])
        self.assertAlmostEqual(multi_day["daily"].cash.iloc[-1], 999506.)
        exits = multi_day["trades"].loc[multi_day["trades"].side == "SELL"]
        np.testing.assert_allclose(exits.execution_notional, [400000., 400000., 150000.])
        self.assertEqual(multi_day["terminal"]["wind_down_start"], str(m.dates[-3].date()))
        self.assertEqual(multi_day["terminal"]["wind_down_days"], 3)
        self.assertEqual(int(multi_day["daily"].terminal_liquidation.sum()), 1)
        self.assertEqual(int(exits.terminal.sum()), 1)
        self.assertTrue(exits.wind_down.all())

    def test_preplanned_exit_still_leaves_three_locked_limit_down_days(self):
        m = market()
        for index, price in zip(range(len(m.dates) - 3, len(m.dates)), [9., 8.10, 7.29]):
            for field in ("open", "high", "low", "close"):
                m.raw[field][index] = price
        m.prepare()
        out = run(m, config(m, liquidate_end=True, wind_down_days=3))
        self.assertFalse(out["terminal"]["fully_liquidated"])
        self.assertEqual(out["terminal"]["residual_positions"], 1)
        self.assertEqual(out["terminal"]["blocked_reasons"]["theoretical_lower_limit"], 1)
        self.assertEqual(out["terminal"]["wind_down_rejections"]["theoretical_lower_limit"], 3)
        self.assertGreater(out["terminal"]["residual_value"], 0.)

    def test_wind_down_window_forbids_buys_and_zero_preserves_old_results(self):
        m = market()
        scores = np.ones_like(m.close)
        scores[:-3] = np.nan  # A new score appears only when exit window starts.
        out = run(m, config(m, liquidate_end=False, wind_down_days=3), scores)
        self.assertTrue(out["trades"].empty)
        self.assertEqual(int(out["daily"].wind_down.sum()), 3)
        self.assertTrue(out["terminal"]["requested"])
        self.assertTrue(out["terminal"]["fully_liquidated"])
        original = run(m, config(m, liquidate_end=True))
        explicit_zero = run(m, config(m, liquidate_end=True, wind_down_days=0))
        pd.testing.assert_frame_equal(original["daily"], explicit_zero["daily"])
        pd.testing.assert_frame_equal(original["trades"], explicit_zero["trades"])
        self.assertFalse(explicit_zero["daily"].wind_down.any())
        with self.assertRaisesRegex(ValueError, "legacy"):
            run(m, config(m, execution_model="legacy", wind_down_days=3))

    def test_capacity_is_minimum_of_lagged_and_posthoc_daily_caps(self):
        m = market()
        m.raw["amt"][65] = 100000.
        m.prepare()
        out = run(m)
        first = out["trades"].iloc[0]
        self.assertAlmostEqual(first.notional, 10000.)
        self.assertLessEqual(out["execution_diagnostics"]["max_realized_daily_participation"], .1 + 1e-12)
        self.assertLessEqual(out["execution_diagnostics"]["max_combined_capacity_excess"], 1e-6)
        self.assertTrue(out["execution_diagnostics"]["posthoc_not_opening_information"])

    def test_actual_fill_notional_controls_capacity_and_sell_taxes(self):
        m = market()
        m.raw["amt"][65] = 100000.
        m.prepare()
        cfg = config(m, slippage=.0005, commission=.0001, liquidate_end=True)
        out = run(m, cfg)
        first = out["trades"].iloc[0]
        # 1,000 shares at10.01 would exceed10,000 actual daily capacity;
        # 900 shares execute9,009 even though raw-open reference is9,000.
        self.assertAlmostEqual(first.raw_share_equivalent, 900.)
        self.assertAlmostEqual(first.execution_notional, 9009.)
        self.assertAlmostEqual(first.commission, .9009)
        self.assertAlmostEqual(first.transfer, .09009)
        sells = out["trades"].loc[out["trades"].side == "SELL"]
        self.assertFalse(sells.empty)
        np.testing.assert_allclose(sells.stamp, sells.execution_notional * .0005)
        np.testing.assert_allclose(sells.transfer, sells.execution_notional * .00001)
        np.testing.assert_allclose(out["trades"].execution_notional, out["trades"].raw_share_equivalent * out["trades"].fill_price_raw)

    def test_factor_jump_blocks_orders_without_destroying_total_return_value(self):
        m = market(price=20.)
        for field in ("open", "high", "low", "close"):
            m.raw[field][70:] /= 2.
        m.raw["cumadj"][70:] *= 2.
        m.prepare()
        out = run(m)
        np.testing.assert_allclose(out["daily"].nav + out["daily"].cost.cumsum(), 1e6)
        day = str(m.dates[70].date())
        self.assertFalse((out["trades"].date == day).any())

    def test_star_minimum_200_then_integer_increment(self):
        m = market(price=30., symbols=("688001.SH",))
        out = run(m, config(m, capital=10000., cash_buffer=0.))
        self.assertAlmostEqual(out["trades"].iloc[0].raw_share_equivalent, 333.)
        too_expensive = market(price=60., symbols=("688001.SH",))
        blocked = run(too_expensive, config(too_expensive, capital=10000., cash_buffer=0.))
        self.assertTrue(blocked["trades"].empty)

    def test_unknown_st_rounding_guard_catches_less_than_4_point_8_percent(self):
        # 2.10 * 1.05 = 2.205 -> half-up2.21 is 5.24%; use2.31:
        # 2.31*1.05=2.4255 ->2.43=5.19%. Search a valid tick with <4.8%.
        previous = next(p / 100 for p in range(200, 500) if (em.round_price_half_up(p / 100 * 1.05) / (p / 100) - 1) < .048)
        m = market(price=previous)
        upper = float(em.round_price_half_up(previous * 1.05))
        for field in ("open", "high", "low", "close"):
            m.raw[field][65] = upper
        m.prepare()
        out = run(m)
        self.assertEqual(out["daily"].trades.iloc[0], 0)
        self.assertIn("unknown_ST_rounded_5pct_upper_guard", out["blocked"])

    def test_outside_daily_range_slipped_fill_is_rejected(self):
        m = market()
        m.raw["high"][:] = 10.
        m.prepare()
        out = run(m, config(m, slippage=.0005))
        self.assertTrue(out["trades"].empty)
        self.assertIn("slipped_price_outside_daily_range_posthoc_audit", out["blocked"])

    def test_never_sells_more_than_owned_or_same_day_new_buy(self):
        m = market()
        scores = np.ones_like(m.close)
        scores[70:] = np.nan
        out = run(m, scores=scores)
        balance = 0.
        bought_dates = set()
        for row in out["trades"].itertuples():
            if row.side == "BUY":
                balance += row.adjusted_units
                bought_dates.add(row.date)
            elif row.side == "SELL":
                self.assertNotIn(row.date, bought_dates)
                balance -= row.adjusted_units
                self.assertGreaterEqual(balance, -1e-7)
        self.assertTrue(np.isfinite(out["daily"].nav).all())
        self.assertTrue((out["daily"].cash >= -1e-6).all())


class MetadataAndUniverseTests(unittest.TestCase):
    def test_true_st_unavailable_strict_fails_closed(self):
        m = market()
        with self.assertRaisesRegex(ValueError, "ST"):
            m.eligible(config(m, st_policy="strict"))
        with self.assertRaisesRegex(ValueError, "ST"):
            run(m, config(m, st_policy="strict"))
        summary = em.metadata_summary(m, config(m), [65, 66])
        self.assertEqual(summary["historical_st_known_observations"], 0)
        self.assertEqual(summary["historical_st_unknown_observations"], 2)

    def test_price_risk_proxy_is_not_true_st_and_is_prefix_causal(self):
        m = market()
        for field in ("open", "high", "low", "close"):
            m.raw[field][65] = 10.5
        m.prepare()
        before = em.price_risk_proxy(m, 20).copy()
        self.assertTrue(before[65, 0])
        self.assertFalse(before[64, 0])
        self.assertTrue(before[84, 0])
        self.assertFalse(before[85, 0])
        for field in ("open", "high", "low", "close"):
            m.raw[field][95:] *= 2.
        m.prepare()
        after = em.price_risk_proxy(m, 20)
        np.testing.assert_array_equal(before[:95], after[:95])
        self.assertFalse(m.eligible(config(m, st_policy="price_proxy"))[65, 0])

    def test_corporate_action_day_does_not_create_false_price_proxy(self):
        m = market()
        for field in ("open", "high", "low", "close"):
            m.raw[field][65] = 9.5
        m.raw["cumadj"][65] = 10 / 9.5
        m.prepare()
        self.assertFalse(em.price_risk_proxy(m, 20)[65, 0])

    def test_board_universe_and_point_in_time_liquidity_pool(self):
        m = market(symbols=("000001.SZ", "300001.SZ", "688001.SH", "302132.SZ"))
        self.assertEqual(list(em.board_labels(m.symbols)), ["main", "growth", "star", "growth"])
        main = m.eligible(config(m, universe="main_a"))
        np.testing.assert_array_equal(main[65], [True, False, False, False])
        m.raw["amt"][:, 1] *= 2
        m.prepare()
        restricted = m.eligible(config(m, universe="growth_a", liquidity_top_n=1))
        np.testing.assert_array_equal(restricted[65], [False, True, False, False])

    def test_new_config_types_and_legacy_defaults(self):
        cfg = lab.Config.from_dict({"execution_model": "daily_conservative", "st_policy": "price_proxy", "universe": "main_a", "liquidate_end": True})
        self.assertTrue(cfg.liquidate_end)
        self.assertEqual(lab.Config().execution_model, "legacy")
        for bad in ({"liquidate_end": 1}, {"risk_proxy_window": -1}, {"liquidity_top_n": 1.2}, {"max_daily_participation": 0},
                    {"wind_down_days": -1}, {"wind_down_days": 121}, {"wind_down_days": True}, {"wind_down_days": 1.5}):
            with self.assertRaises(ValueError):
                lab.Config.from_dict(bad)


if __name__ == "__main__":
    unittest.main()
