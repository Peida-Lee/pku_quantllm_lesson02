"""Hand calculations, prefix counterfactuals, and hostile JSON rejection."""
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import strategy_blocks as blocks

ROOT = Path(blocks.__file__).parent


def node(op, *args, **params):
    return {"op": op, "args": list(args), "params": params}


def document(score=None, filt=None, packs=None, selection=None):
    doc = {"format": "daily-factor-strategy", "version": 1, "name": "Synthetic test",
           "score": score or node("close"), "selection": {} if selection is None else selection,
           "packs": [] if packs is None else packs}
    if filt is not None:
        doc["filter"] = filt
    return doc


def market(close=None):
    if close is None:
        t = np.arange(90, dtype=float)[:, None]
        j = np.arange(4, dtype=float)[None, :]
        close = (10 + j) * np.exp(t / 1000) * (1 + .03 * np.sin(t / 3 + j))
    close = np.asarray(close, dtype=float)
    if close.ndim == 1:
        close = close[:, None]
    n, k = close.shape
    t = np.arange(n, dtype=float)[:, None]
    j = np.arange(k, dtype=float)[None, :]
    raw = {"close": close.copy(), "open": close * .999, "high": close * 1.01,
           "low": close * .99, "amt": 1e8 + t * 1e6 + j * 1e7,
           "num_trades": 1000 + t * 10 + j * 20, "cumadj": np.ones_like(close)}
    returns = np.full_like(close, np.nan)
    returns[1:] = close[1:] / close[:-1] - 1
    return SimpleNamespace(raw=raw, close=close.copy(), open=raw["open"].copy(),
                           scale=np.ones_like(close), returns=returns,
                           history=np.broadcast_to(np.arange(1, n + 1)[:, None], close.shape).copy(),
                           eligible=lambda cfg: np.ones_like(close, dtype=bool))


CFG = SimpleNamespace(top_n=7, rebalance="monthly")


def evaluate(expr, m=None, filt=None):
    return blocks.compile_strategy(m or market(), CFG, document(expr, filt))["score"]


class HandCalculationTests(unittest.TestCase):
    def test_trailing_mean_std_and_window_warmup(self):
        m = market([1., 2., 3., 4., 5.])
        mean = evaluate(node("rolling_mean", node("close"), window=3), m)[:, 0]
        std = evaluate(node("rolling_std", node("close"), window=3), m)[:, 0]
        np.testing.assert_allclose(mean, [np.nan, np.nan, 2., 3., 4.], equal_nan=True)
        np.testing.assert_allclose(std, [np.nan, np.nan, 1., 1., 1.], equal_nan=True)

    def test_lag_delta_pct_change_and_zero_period(self):
        m = market([1., 2., 4., 8.])
        np.testing.assert_allclose(evaluate(node("lag", node("close"), period=0), m)[:, 0], [1, 2, 4, 8])
        np.testing.assert_allclose(evaluate(node("delta", node("close"), period=2), m)[:, 0], [np.nan, np.nan, 3, 6], equal_nan=True)
        np.testing.assert_allclose(evaluate(node("pct_change", node("close"), period=2), m)[:, 0], [np.nan, np.nan, 3, 3], equal_nan=True)

    def test_covariance_correlation_and_linear_weights(self):
        m = market([1., 2., 3., 4.])
        x = node("close")
        y = node("multiply", x, node("constant", value=2))
        np.testing.assert_allclose(evaluate(node("rolling_cov", x, y, window=3), m)[2:, 0], [2., 2.])
        np.testing.assert_allclose(evaluate(node("rolling_corr", x, y, window=3), m)[2:, 0], [1., 1.])
        self.assertAlmostEqual(evaluate(node("linear_decay", x, window=3), m)[2, 0], 14 / 6)

    def test_unbiased_skew_and_excess_kurtosis_match_independent_window_formula(self):
        values = np.array([1., 1., 2., 3., 8., 13.])
        m = market(values)
        tail = values[-5:]
        centered = tail - tail.mean()
        m2, m3, m4 = [np.mean(centered ** power) for power in (2, 3, 4)]
        n = len(tail)
        skew = np.sqrt(n * (n - 1)) / (n - 2) * m3 / m2 ** 1.5
        kurt = (n - 1) / ((n - 2) * (n - 3)) * ((n + 1) * (m4 / m2 ** 2 - 3) + 6)
        self.assertAlmostEqual(evaluate(node("rolling_skew", node("close"), window=5), m)[-1, 0], skew)
        self.assertAlmostEqual(evaluate(node("rolling_kurt", node("close"), window=5), m)[-1, 0], kurt)

    def test_filter_precedes_score_cross_section(self):
        m = market([[1., 2., 100.], [2., 4., 200.]])
        filt = node("less", node("close"), node("constant", value=10))
        out = evaluate(node("cs_rank", node("close")), m, filt)
        np.testing.assert_allclose(out, [[.5, 1., np.nan], [.5, 1., np.nan]], equal_nan=True)

    def test_filter_cross_sections_use_base_universe(self):
        m = market([[1., 2., 100.]])
        filt = node("less_equal", node("cs_rank", node("close")), node("constant", value=.7))
        out = evaluate(node("cs_rank", node("close")), m, filt)
        np.testing.assert_allclose(out, [[.5, 1., np.nan]], equal_nan=True)

    def test_missing_numeric_and_boolean_are_not_zero_filled(self):
        m = market([[np.nan, 0., 2.]])
        filt = node("not", node("greater", node("close"), node("constant", value=1)))
        out = evaluate(node("constant", value=9), m, filt)
        np.testing.assert_allclose(out, [[np.nan, 9., np.nan]], equal_nan=True)
        np.testing.assert_allclose(evaluate(node("divide", node("close"), node("constant", value=0)), m), [[np.nan] * 3], equal_nan=True)
        # NumPy alone defines NaN**0 as 1; strategy missingness still propagates.
        self.assertTrue(np.isnan(evaluate(node("power", node("close"), node("constant", value=0)), m)[0, 0]))

    def test_rolling_window_missing_requires_complete_window(self):
        m = market([1., np.nan, 3., 4., 5.])
        actual = evaluate(node("rolling_mean", node("close"), window=3), m)[:, 0]
        np.testing.assert_allclose(actual, [np.nan, np.nan, np.nan, np.nan, 4.], equal_nan=True)

    def test_tied_recent_extreme_returns_days_since_most_recent(self):
        m = market([5., 1., 5., 2.])
        self.assertEqual(evaluate(node("rolling_argmax", node("close"), window=3), m)[2, 0], 0)
        self.assertEqual(evaluate(node("rolling_argmin", node("close"), window=3), m)[3, 0], 2)

    def test_ascending_direction_and_selection_inheritance(self):
        m = market([1., 2., 3.])
        out = blocks.compile_strategy(m, CFG, document(selection={"direction": "asc"}))
        np.testing.assert_allclose(out["score"][:, 0], [-1., -2., -3.])
        self.assertEqual(out["document"]["selection"], {"top_n": 7, "rebalance": "monthly", "direction": "asc"})
        explicit = blocks.compile_strategy(m, CFG, document(selection={"top_n": 3, "rebalance": "daily", "direction": "desc"}))
        self.assertEqual(explicit["meta"]["selection"]["top_n"], 3)

    def test_duplicate_subtrees_share_bounded_cache(self):
        x = node("rolling_mean", node("close"), window=3)
        score = node("add", x, copy.deepcopy(x))
        with patch.object(blocks, "_evaluate_uncached", wraps=blocks._evaluate_uncached) as implementation:
            evaluate(score)
            observed = [call.args[0]["op"] for call in implementation.call_args_list]
        self.assertEqual(observed.count("rolling_mean"), 1)
        self.assertEqual(observed.count("close"), 1)


class CatalogCoverageTests(unittest.TestCase):
    def test_all_catalog_defaults_execute_and_have_finite_output(self):
        catalog = blocks.get_catalog()
        self.assertGreaterEqual(len(catalog["blocks"]), 50)
        self.assertEqual(len({b["id"] for b in catalog["blocks"]}), len(catalog["blocks"]))
        m = market()
        for block in catalog["blocks"]:
            with self.subTest(operation=block["id"]):
                for required in ("id", "label", "category", "description", "inputs", "params", "output", "default"):
                    self.assertIn(required, block)
                expr = block["default"]
                if block["output"] == "boolean":
                    expr = node("where", expr, node("constant", value=1), node("constant", value=0))
                out = evaluate(expr, m)
                self.assertEqual(out.shape, m.close.shape)
                self.assertFalse(np.isinf(out).any())
                self.assertTrue(np.isfinite(out[-10:]).any())

    def test_every_module_is_prefix_causal_under_future_counterfactual(self):
        baseline, altered = market(), market()
        for value in altered.raw.values():
            value[70:] *= 3.
        altered.close[70:] *= 3.
        altered.open[70:] *= 3.
        altered.returns[70:] += 1.
        altered.history[70:] += 100
        altered.scale[70:] *= 2.
        for block in blocks.get_catalog()["blocks"]:
            with self.subTest(operation=block["id"]):
                expr = block["default"]
                if block["output"] == "boolean":
                    expr = node("where", expr, node("constant", value=1), node("constant", value=0))
                a, b = evaluate(expr, baseline), evaluate(expr, altered)
                np.testing.assert_allclose(a[:70], b[:70], equal_nan=True, rtol=0, atol=0)

    def test_optional_trade_count_remains_missing(self):
        m = market()
        del m.raw["num_trades"]
        self.assertTrue(np.isnan(evaluate(node("trade_count"), m)).all())

    def test_catalog_file_matches_runtime_definition(self):
        actual = json.loads((ROOT / "block_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, blocks.get_catalog())


class CustomPackTests(unittest.TestCase):
    def setUp(self):
        self.pack = json.loads((ROOT / "examples" / "short_reversal_pack.json").read_text(encoding="utf-8"))

    def test_pack_accepts_arbitrary_series_input_and_matches_hand_calculation(self):
        expr = node("custom.short_reversal5", node("multiply", node("close"), node("constant", value=2)))
        m = market(np.arange(1., 10.))
        score = blocks.compile_strategy(m, CFG, document(expr, packs=[self.pack]))["score"]
        expected = -(m.close[5:] / m.close[:-5] - 1)
        np.testing.assert_allclose(score[5:], expected)

    def test_custom_catalog_default_compiles_and_pack_round_trip(self):
        normalized = blocks.validate_block_pack(self.pack)["pack"]
        self.assertEqual(blocks.import_block_pack(blocks.export_block_pack(normalized))["pack"], normalized)
        custom = blocks.get_catalog([normalized])["blocks"][-1]
        out = blocks.compile_strategy(market(), CFG, document(custom["default"], packs=[normalized]))
        self.assertTrue(np.isfinite(out["score"]).any())
        exported = blocks.export_strategy(out["document"])
        self.assertEqual(blocks.import_strategy(exported)["strategy"], out["document"])

    def test_all_examples_validate_and_compile(self):
        count = 0
        for path in (ROOT / "examples").glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(example=path.name):
                if value["format"] == "daily-factor-strategy":
                    self.assertTrue(np.isfinite(blocks.compile_strategy(market(), CFG, value)["score"]).any())
                    count += 1
                else:
                    blocks.validate_block_pack(value)
        self.assertGreaterEqual(count, 3)

    def test_nested_macros_and_boolean_macro(self):
        pack = copy.deepcopy(self.pack)
        pack["blocks"].append({"id": "custom.negative", "name": "Negative", "inputs": [{"name": "x", "type": "series"}],
                               "template": node("less", node("input", name="x"), node("constant", value=0))})
        expr = node("custom.short_reversal5", node("close"))
        filt = node("custom.negative", expr)
        out = blocks.compile_strategy(market(np.arange(1., 12.)), CFG, document(expr, filt, [pack]))
        self.assertTrue(np.isfinite(out["score"][5:]).all())


class SecurityValidationTests(unittest.TestCase):
    def reject(self, value):
        with self.assertRaises(blocks.StrategyError):
            blocks.validate_strategy(value)

    def test_reject_unknown_or_executable_operations_and_keys(self):
        for op in ("eval", "exec", "__import__", "future_close", "fetch", "os.system"):
            with self.subTest(op=op):
                self.reject(document(node(op)))
        for key in ("code", "python", "script", "__proto__", "constructor", "__import__"):
            doc = document()
            doc["score"][key] = "harmless-looking text"
            self.reject(doc)

    def test_reject_negative_or_excessive_windows_lags_and_nonfinite_values(self):
        for op, param, value in [("lag", "period", -1), ("rolling_mean", "window", -5),
                                 ("rolling_mean", "window", 253), ("lag", "period", 1.5),
                                 ("rolling_mean", "window", True), ("lag", "period", 1000000)]:
            self.reject(document(node(op, node("close"), **{param: value})))
        for value in (np.nan, np.inf, -np.inf):
            self.reject(document(node("constant", value=value)))
        self.reject(document(node("constant", value=10 ** 400)))

    def test_reject_type_mismatch_unknown_parameter_and_bad_selection(self):
        self.reject(document(node("greater", node("close"), node("constant", value=1))))
        self.reject(document(node("close"), node("close")))
        self.reject(document(node("add", node("is_finite", node("close")), node("close"))))
        self.reject(document(node("close", arbitrary=1)))
        self.reject(document(selection={"top_n": True}))
        self.reject(document(selection={"rebalance": "hourly"}))

    def test_reject_direct_json_cycles_and_excessive_depth_size(self):
        expr = node("negate")
        expr["args"].append(expr)
        self.reject(document(expr))
        expr = node("close")
        for _ in range(20):
            expr = node("negate", expr)
        self.reject(document(expr))
        huge = document()
        huge["name"] = "x" * 200000
        self.reject(huge)

    def test_reject_recursive_and_unused_malicious_custom_definitions(self):
        pack = json.loads((ROOT / "examples" / "short_reversal_pack.json").read_text(encoding="utf-8"))
        pack["blocks"][0]["template"] = node("custom.short_reversal5", node("input", name="x"))
        self.reject(document(packs=[pack]))
        pack["blocks"][0]["template"] = node("exec")
        self.reject(document(packs=[pack]))

    def test_reject_mutual_macro_cycles_and_duplicate_ids(self):
        pack = {"format": "daily-factor-block-pack", "version": 1, "id": "bad", "name": "Bad", "blocks": [
            {"id": "custom.a", "name": "A", "inputs": [], "template": node("custom.b")},
            {"id": "custom.b", "name": "B", "inputs": [], "template": node("custom.a")}]}
        self.reject(document(packs=[pack]))
        pack["blocks"][0]["template"] = node("close")
        pack["blocks"][1]["id"] = "custom.a"
        self.reject(document(packs=[pack]))

    def test_reject_macro_expansion_explosion(self):
        pack = {"format": "daily-factor-block-pack", "version": 1, "id": "expand", "name": "Expand", "blocks": [
            {"id": "custom.double", "name": "Double", "inputs": [{"name": "x", "type": "series"}],
             "template": node("add", node("input", name="x"), node("input", name="x"))}]}
        expr = node("close")
        for _ in range(9):
            expr = node("custom.double", expr)
        self.reject(document(expr, packs=[pack]))
        pack["blocks"].append({"id": "custom.unused_big", "name": "Unused expansion", "inputs": [], "template": expr})
        self.reject(document(packs=[pack]))

    def test_validate_is_static_and_does_not_modify_input_document(self):
        original = document(node("rolling_mean", node("close")))
        preserved = copy.deepcopy(original)
        with patch.object(blocks, "_evaluate", side_effect=AssertionError("Must remain static")):
            validation = blocks.validate_strategy(original)
        self.assertEqual(original, preserved)
        self.assertEqual(validation["strategy"]["score"]["params"]["window"], 20)


if __name__ == "__main__":
    unittest.main()
