"""Independent small-market accounting and counterfactual execution checks.

Run: python -m unittest discover -s tests -v
No archive reads, network, or real orders. Prices/amounts are synthetic.
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab


def market(price=10., n=26):
    m = lab.Market.__new__(lab.Market)
    m.dates = pd.bdate_range("2024-01-01", periods=n)
    m.symbols = np.array(["000001.SZ"])
    m.raw = {k: np.full((n, 1), price, dtype=float)
             for k in ("open", "high", "low", "close")}
    m.raw["amt"] = np.full((n, 1), 1e9)
    m.raw["cumadj"] = np.ones((n, 1))
    # Supply known preceding liquidity history for these short execution-only
    # fixtures. Factor eligibility is tested separately on actual rolling data.
    def fixture_prepare():
        lab.Market.prepare(m)
        m.amount20[:19] = 1e9
        m.active20[:19] = 20.
    m.prepare = fixture_prepare
    m.prepare()
    return m


def config(m, **overrides):
    args = dict(start=str(m.dates[1].date()), end=str(m.dates[-1].date()),
                capital=10000., top_n=1, rebalance="weekly", cash_buffer=0.,
                max_participation=.1, open_guard=.3, min_history=65,
                commission=0., min_commission=0., slippage=0.)
    args.update(overrides)
    return lab.Config(**args).validate()


def run(m, cfg=None, score=None):
    return lab.simulate(m, np.ones_like(m.open) if score is None else score,
                        config(m) if cfg is None else cfg, cost_multiplier=0.)


class FeeChecks(unittest.TestCase):
    def test_2024_sell_fee_hand_calculation(self):
        # 10,000 * (commission .0003 + slip .0005 + stamp .0005 + transfer .00001).
        f = lab.costs(10000., "SELL", pd.Timestamp("2024-01-02"), lab.Config())
        self.assertAlmostEqual(f["total"], 15.1)
        self.assertAlmostEqual(f["commission"], 5.)
        self.assertAlmostEqual(f["stamp"], 5.)

    def test_stamp_and_transfer_change_dates(self):
        cfg = lab.Config(commission=0., min_commission=0., slippage=0.)
        self.assertAlmostEqual(lab.costs(100000., "SELL", pd.Timestamp("2023-08-27"), cfg)["stamp"], 100.)
        self.assertAlmostEqual(lab.costs(100000., "SELL", pd.Timestamp("2023-08-28"), cfg)["stamp"], 50.)
        self.assertAlmostEqual(lab.costs(100000., "BUY", pd.Timestamp("2022-04-28"), cfg)["transfer"], 2.)
        self.assertAlmostEqual(lab.costs(100000., "BUY", pd.Timestamp("2022-04-29"), cfg)["transfer"], 1.)

    def test_affordability_matches_real_fee_function(self):
        cfg = lab.Config()
        date = pd.Timestamp("2024-01-02")
        for budget in (1., 5., 10., 1000., 100000.):
            for mult in (1., 2.):
                notional = lab.affordable(budget, 1e10, date, cfg, mult)
                fee = lab.costs(notional, "BUY", date, cfg, mult)["total"]
                self.assertLessEqual(notional + fee, budget + 1e-8)


class AccountingChecks(unittest.TestCase):
    def test_flat_price_no_cost_nav(self):
        out = run(market())
        np.testing.assert_allclose(out["daily"].nav, 10000.)
        self.assertEqual(out["metrics"]["total_return"], 0.)

    def test_close_price_gain_hand_calculation(self):
        m = market()
        # 1,000 shares at opening 10, closing 11 on execution day -> 11,000 NAV.
        for key in ("high", "close"):
            m.raw[key][1:, 0] = 11.
        m.raw["open"][2:, 0] = 11.
        m.raw["low"][2:, 0] = 11.
        m.prepare()
        out = run(m)
        self.assertAlmostEqual(out["daily"].nav.iloc[0], 11000.)
        self.assertAlmostEqual(out["daily"].nav.iloc[-1], 11000.)

    def test_round_trip_actual_minimum_fees_and_lots_reconcile_cash(self):
        m = market()
        cfg = config(m, commission=.0003, min_commission=5., slippage=.0005)
        score = np.ones_like(m.open)
        score[4:, :] = np.nan  # Exit on next Monday at a price of 10.
        out = lab.simulate(m, score, cfg)
        # 10,000 cannot fund 1,000 shares plus fees: buy 900 shares.
        # Buy: 9,000 + 5 commission + 4.5 slippage + .09 transfer.
        # Sell: 9,000 - 5 commission - 4.5 slippage - 4.5 stamp - .09 transfer.
        self.assertEqual(list(out["trades"].side), ["BUY", "SELL"])
        self.assertAlmostEqual(out["trades"].iloc[0].raw_share_equivalent, 900.)
        self.assertAlmostEqual(out["daily"].nav.iloc[-1], 9976.32)
        self.assertAlmostEqual(out["daily"].cost.sum(), 23.68)
        self.assertAlmostEqual(out["daily"].cash.iloc[-1], 9976.32)

    def test_two_for_one_split_preserves_nav_and_doubles_raw_shares(self):
        m = market(price=20.)
        for key in ("open", "high", "low", "close"):
            m.raw[key][2:, 0] = 10.
        m.raw["cumadj"][2:, 0] = 2.
        m.prepare()
        out = run(m)
        np.testing.assert_allclose(out["daily"].nav, 10000.)
        self.assertAlmostEqual(out["holdings"].adjusted_units.iloc[0], 500.)
        self.assertAlmostEqual(out["holdings"].adjusted_units.iloc[0] * m.scale[-1, 0], 1000.)

    def test_missing_new_purchase_close_uses_known_open_valuation(self):
        m = market()
        m.raw["close"][1, 0] = np.nan
        m.prepare()
        out = run(m)
        # Bought 1,000 shares at a known 10. No later closing observation does
        # not make their first valuation zero before the configured writeoff.
        self.assertAlmostEqual(out["daily"].nav.iloc[0], 10000.)

    def test_writeoff_occurs_at_configured_missing_observation_delay(self):
        m = market()
        for key in ("open", "high", "low", "close"):
            m.raw[key][2:, 0] = np.nan
        m.prepare()
        out = run(m, config(m, writeoff_missing_days=3))
        np.testing.assert_allclose(out["daily"].nav.iloc[:3], [10000., 10000., 10000.])
        self.assertAlmostEqual(out["daily"].nav.iloc[3], 0.)
        self.assertAlmostEqual(out["daily"].writeoff_loss.sum(), 10000.)
        self.assertTrue(np.isfinite(out["daily"]["return"]).all())
        self.assertTrue(np.isfinite(out["metrics"]["annual_vol"]))
        self.assertTrue(np.isfinite(out["metrics"]["sharpe"]))

    def test_drawdown_includes_initial_capital(self):
        daily = pd.DataFrame(dict(nav=[9000., 9500.], cost=[0., 0.], turnover=[0., 0.],
                                  invested_fraction=[1., 1.], trades=[0, 0],
                                  writeoff_loss=[0., 0.], stale_fraction=[0., 0.]))
        self.assertAlmostEqual(lab.performance(daily, 10000.)["max_drawdown"], -.1)


class CausalityAndExecutionChecks(unittest.TestCase):
    def test_future_market_observations_do_not_change_prefix(self):
        baseline = market()
        altered = market()
        for key in ("open", "high", "low", "close", "cumadj", "amt"):
            altered.raw[key][13:, 0] *= 2.
        altered.prepare()
        a, b = run(baseline), run(altered)
        pd.testing.assert_frame_equal(a["daily"].iloc[:12], b["daily"].iloc[:12])

    def test_current_close_signal_not_used_for_current_open(self):
        m = market()
        s1 = np.ones_like(m.open)
        s2 = s1.copy()
        s2[1:, :] = np.nan
        a, b = run(m, score=s1), run(m, score=s2)
        pd.testing.assert_series_equal(a["daily"].iloc[0], b["daily"].iloc[0])
        self.assertEqual(b["trades"].iloc[0].signal_date, "2024-01-01")

    def test_limit_like_open_jump_blocks_purchase(self):
        m = market()
        for key in ("open", "high", "low", "close"):
            m.raw[key][1:, 0] = 11.
        m.prepare()
        cfg = config(m, open_guard=.048)
        out = run(m, cfg)
        self.assertEqual(out["daily"].trades.iloc[0], 0)
        self.assertEqual(out["daily"].cash.iloc[0], 10000.)

    def test_blocked_top_rank_does_not_fall_back_to_second_rank(self):
        m = market()
        m.symbols = np.array(["000001.SZ", "000002.SZ"])
        m.raw = {key: np.repeat(value, 2, axis=1) for key, value in m.raw.items()}
        for key in ("open", "high", "low", "close"):
            m.raw[key][1:, 0] = 11.
        m.prepare()
        score = np.tile([2., 1.], (len(m.dates), 1))
        out = run(m, config(m, open_guard=.048), score)
        self.assertEqual(out["daily"].trades.iloc[0], 0)
        self.assertEqual(out["daily"].cash.iloc[0], 10000.)
        self.assertFalse((out["trades"].symbol == "000002.SZ").any())

    def test_unfilled_order_retries_before_next_week(self):
        m = market()
        m.raw["open"][1, 0] = np.nan
        m.prepare()
        out = run(m)
        self.assertFalse(out["trades"].empty)
        # Tuesday's missing open should delay its selected order until Wed,
        # without waiting for Monday's next scheduled rebalance.
        self.assertEqual(out["trades"].iloc[0].date, str(m.dates[2].date()))

    def test_main_board_purchases_are_whole_hundred_share_lots(self):
        m = market(price=30.)
        out = run(m)
        buys = out["trades"].loc[out["trades"].side == "BUY"]
        self.assertFalse(buys.empty)
        for qty in buys.raw_share_equivalent:
            self.assertAlmostEqual(qty / 100., round(qty / 100.), places=8)

    def test_star_board_purchase_below_200_shares_is_rejected(self):
        m = market(price=60.)
        m.symbols[0] = "688001.SH"
        out = run(m)
        # Budget 10,000 cannot fund the STAR minimum 200 * 60 = 12,000.
        self.assertTrue(out["trades"].empty)
        np.testing.assert_allclose(out["daily"].nav, 10000.)

    def test_daily_amount_gate_does_not_change_score_or_substitute_stock(self):
        m = market()
        m.raw["amt"][1, 0] = 0.
        m.prepare()
        score = np.ones_like(m.open)
        before = score.copy()
        out = run(m, score=score)
        np.testing.assert_array_equal(score, before)
        self.assertEqual(out["daily"].trades.iloc[0], 0)
        self.assertEqual(out["trades"].iloc[0].date, str(m.dates[2].date()))


if __name__ == "__main__":
    unittest.main()
