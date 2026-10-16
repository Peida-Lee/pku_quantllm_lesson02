"""Original-ZIP SMALL-SLICE integration checks; not a full-grid backtest.

Reads selected columns from each original Feather once, keeps only
2023-06-01..2025-03-31 and 48 deterministically spaced source symbols.
No archive hash is recomputed and no extracted source file is written.
"""
import os
for _key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_key] = "1"

from pathlib import Path
import sys
import unittest
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab
import research
import research_v4 as rv4
import strategy_blocks as sb
import v4_factors as vf

SLICE_REPORT = {}


def load_original_slice():
    """Select Arrow columns before converting to numpy; never instantiate full Market."""
    resource = research.reserve_performance_cores()
    if os.name == "nt" and len(resource.get("reserved_logical_processors", [])) < 2:
        raise RuntimeError("Cannot establish two-performance-core reservation: " + str(resource))
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    source = Path(__file__).resolve().parents[2] / "feathers.zip"
    m = lab.Market.__new__(lab.Market)
    m.source, m.raw = str(source), {}
    rows = None
    with zipfile.ZipFile(source) as z:
        for name in vf.SOURCE_FIELDS:
            payload = z.read("feathers/" + name + ".feather")
            reader = pa.ipc.open_file(pa.BufferReader(payload))
            all_symbols = reader.schema.names[1:]
            if rows is None:
                positions = np.unique(np.linspace(0, len(all_symbols)-1, 48, dtype=int))
                symbols = [all_symbols[i] for i in positions]
            table = feather.read_table(pa.BufferReader(payload), columns=["date", *symbols], use_threads=False)
            dates = pd.DatetimeIndex(table["date"].to_numpy())
            if rows is None:
                rows = np.flatnonzero((dates >= "2023-06-01") & (dates <= "2025-03-31"))
                m.dates, m.symbols = dates[rows], np.asarray(symbols)
            else:
                np.testing.assert_array_equal(m.dates, dates[rows])
                np.testing.assert_array_equal(table.column_names[1:], m.symbols)
            m.raw[name] = np.column_stack([table[s].to_numpy()[rows] for s in symbols])
            del table, reader, payload
    m.prepare()
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    SLICE_REPORT.update(source=str(source), source_bytes=source.stat().st_size,
                        rows=len(m.dates), symbols=len(m.symbols),
                        first_date=str(m.dates[0].date()), last_date=str(m.dates[-1].date()),
                        resource=resource, numeric_threads=1, arrow_threads=1,
                        full_grid_backtest=False, archive_hash_recomputed=False)
    return m


def prepared_copy(m, future=False):
    other = lab.Market.__new__(lab.Market)
    other.source, other.dates, other.symbols = m.source, m.dates.copy(), m.symbols.copy()
    other.raw = {k:v.copy() for k,v in m.raw.items()}
    if future:
        later = other.dates >= "2025-01-01"
        for key in vf.SOURCE_FIELDS:
            other.raw[key][later] *= 1.731
    other.prepare()
    return other


def candidate(fid):
    return next(c for c in rv4.grid() if c["factor_id"] == fid and c["universe"] == "all_liquid"
                and c["top_n"] == 20 and c["rebalance"] == "weekly")


def cfg_for(fid):
    return rv4.config_for(candidate(fid), "selection_2024")


class OriginalSliceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_original_slice()
        cls.future = prepared_copy(cls.m, future=True)

    def test_all_78_coverage_prefix_and_2024_signatures(self):
        m, other = self.m, self.future
        engine, future_engine = vf.FactorEngine(m), vf.FactorEngine(other)
        past = m.dates < "2025-01-01"
        train = (m.dates >= "2024-01-01") & past
        coverage = []
        for fid, spec in vf.FACTOR_SPECS.items():
            with self.subTest(factor=fid):
                a, b = engine.compute(fid), future_engine.compute(fid)
                np.testing.assert_array_equal(np.isfinite(a[past]), np.isfinite(b[past]))
                np.testing.assert_allclose(a[past], b[past], atol=1e-11, rtol=1e-11, equal_nan=True)
                cfg = cfg_for(fid)
                eligibility = m.eligible(cfg)
                x = np.where(eligibility, a, np.nan)
                y = np.where(other.eligible(cfg), b, np.nan)
                sig1, sig2 = rv4.signal_signature(m, x, cfg), rv4.signal_signature(other, y, cfg)
                self.assertEqual(sig1, sig2)
                finite = np.isfinite(a[train])
                count = int(finite.sum())
                self.assertGreater(count, 0)
                coverage.append({"id":fid, "finite_training_cells":count,
                                 "training_cells":int(finite.size),
                                 "finite_eligible_training_cells":int(np.isfinite(x[train]).sum()),
                                 "signature_unchanged":True})
        SLICE_REPORT["coverage"] = coverage
        SLICE_REPORT["future_perturbation_all_specs"] = True
        SLICE_REPORT["training_signatures_unchanged"] = 78

    def test_seven_baselines_on_legal_complete_input_intersection(self):
        old = research.factor_matrices(self.m)
        engine = vf.FactorEngine(self.m)
        counts = {}
        for old_id, fid in vf.BASELINE_EQUIVALENTS.items():
            with self.subTest(factor=fid):
                x, y = engine.compute(fid), old[old_id]
                legal = np.isfinite(x)
                self.assertGreater(int(legal.sum()), 0)
                self.assertTrue(np.isfinite(y[legal]).all())
                np.testing.assert_allclose(x[legal], y[legal], atol=1e-11, rtol=1e-11)
                counts[old_id] = int(legal.sum())
        SLICE_REPORT["baseline_valid_cells"] = counts

    def test_all_registered_blocks_compile_with_exact_factor_validity(self):
        ids = {x["id"] for x in sb.catalog()["blocks"]}
        engine = vf.FactorEngine(self.m)
        for fid in vf.FACTOR_SPECS:
            with self.subTest(factor=fid):
                self.assertIn("v4_"+fid, ids)
                cfg = cfg_for(fid)
                compiled = sb.compile_strategy(self.m, cfg, rv4.strategy_document(candidate(fid)))
                expected = engine.compute(fid, self.m.eligible(cfg))
                np.testing.assert_array_equal(np.isfinite(compiled["score"]), np.isfinite(expected))
                np.testing.assert_allclose(compiled["score"], expected, rtol=0, atol=0, equal_nan=True)
        unknown = rv4.strategy_document(candidate("reversal_w005"))
        unknown["score"]["op"] = "v4_unknown_w020"
        with self.assertRaises(sb.StrategyError):
            sb.compile_strategy(self.m, cfg_for("reversal_w005"), unknown)
        malformed = rv4.strategy_document(candidate("reversal_w005"))
        malformed["score"]["params"] = {"window":20}
        with self.assertRaises(sb.StrategyError):
            sb.compile_strategy(self.m, cfg_for("reversal_w005"), malformed)
        SLICE_REPORT["registered_blocks_exact_match"] = 78
        SLICE_REPORT["unknown_and_extra_parameter_ops_rejected"] = True

    def test_actual_original_missingness_and_injected_hole_never_fill_zero(self):
        raw_invalid = {k:int((~np.isfinite(v)).sum()) for k,v in self.m.raw.items()}
        altered = prepared_copy(self.m)
        t = int(np.flatnonzero(altered.dates >= "2024-06-03")[0])
        j = int(np.flatnonzero(np.isfinite(altered.raw["close"][t]) & (altered.raw["close"][t] > 0))[0])
        altered.raw["close"][t, j] = np.nan
        # The current overnight gap uses O[t] and C[t-1], not C[t].
        # Invalidate its actual current input instead of demanding a spurious
        # dependency on the close that is not part of that formula.
        altered.raw["open"][t, j] = np.nan
        altered.raw["amt"][t, j] = np.nan
        altered.raw["num_trades"][t, j] = np.nan
        altered.prepare()
        engine = vf.FactorEngine(altered)
        checked = 0
        for fid, spec in vf.FACTOR_SPECS.items():
            # Every current family depends on at least one of the injected fields.
            if any(k in spec["required_fields"] for k in ("close", "amt", "num_trades")):
                self.assertTrue(np.isnan(engine.compute(fid)[t, j]), fid)
                checked += 1
        self.assertEqual(checked, 78)
        SLICE_REPORT["original_nonfinite_source_cells"] = raw_invalid
        SLICE_REPORT["injected_hole_invalidates_specs"] = checked

    def test_actual_2024_order_target_prefix_ignores_future_2025_mutation(self):
        # Full detailed orders on a small symbol slice, 3 declared baselines only.
        # This checks actual frozen target units, not just sorted signal names.
        counts = {}
        for fid in ("reversal_w005", "lowvol_w020", "lowrange_w020"):
            with self.subTest(factor=fid):
                cfg = cfg_for(fid)
                results = []
                for m in (self.m, self.future):
                    score = vf.FactorEngine(m).compute(fid, m.eligible(cfg))
                    results.append(lab.simulate(m, score, cfg, keep_trades=True))
                a, b = results
                self.assertGreater(len(a["orders"]), 0)
                self.assertIn("target_adjusted_units", a["orders"].columns)
                pd.testing.assert_frame_equal(a["orders"], b["orders"], check_exact=True)
                pd.testing.assert_frame_equal(a["daily"], b["daily"], check_exact=True)
                counts[fid] = len(a["orders"])
        SLICE_REPORT["actual_target_order_rows_unchanged"] = counts


if __name__ == "__main__":
    import json
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(OriginalSliceIntegrationTests))
    print("SLICE_INTEGRATION_REPORT=" + json.dumps(SLICE_REPORT, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
