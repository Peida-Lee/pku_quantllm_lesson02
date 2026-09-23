import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

spec = importlib.util.spec_from_file_location("gap_homework", Path(__file__).with_name("run.py"))
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


def fixture_df():
    rows = []
    for s, base in [("A", 10), ("B", 100)]:
        for i in range(35):
            price = base + i + np.sin(i)
            rows.append({"datetime": datetime(2020, 1, 1) + timedelta(days=i), "vt_symbol": s,
                         "open": price, "close": price + .5, "volume": float(i % 7 + 1)})
    return pl.DataFrame(rows).sort("vt_symbol", "datetime")


def test_calendar_label_does_not_skip_suspension():
    df = fixture_df().filter(~((pl.col("vt_symbol") == "A") & (pl.col("datetime") == datetime(2020, 1, 2))))
    labels = run.make_labels(df)
    row = labels.filter((pl.col("vt_symbol") == "A") & (pl.col("datetime") == datetime(2020, 1, 1))).row(0, named=True)
    assert row["label"] is None


def test_membership_intervals_and_overlap_do_not_duplicate():
    df = fixture_df()
    filters = {"A": [(datetime(2020, 1, 2), datetime(2020, 1, 3)), (datetime(2020, 1, 3), datetime(2020, 1, 3))]}
    keys = run.eligible_keys(df, filters)
    assert keys.height == 2 and keys["vt_symbol"].unique().to_list() == ["A"]


def test_labels_do_not_cross_segment_end():
    labels = run.make_labels(fixture_df())
    sliced = run.segment_panel(labels, "2020-01-01", "2020-01-05")
    assert sliced["datetime"].max() == datetime(2020, 1, 2)
    assert sliced["exit_date"].max() <= datetime(2020, 1, 5)


def test_optimized_rolling_matches_teacher_on_nulls_and_nan():
    from vnpy.alpha.dataset.ts_function import ts_mean as original_mean, ts_std as original_std
    from vnpy.alpha.dataset.utility import DataProxy
    df = fixture_df().select("datetime", "vt_symbol", pl.col("close").alias("data"))
    df = df.with_columns(pl.when(pl.col("datetime").dt.day() == 4).then(None)
                        .when(pl.col("datetime").dt.day() == 6).then(float("nan"))
                        .otherwise(pl.col("data")).alias("data"))
    for original, optimized in [(original_mean, run.ts_mean), (original_std, run.ts_std)]:
        a, b = [func(DataProxy(df), 5).df["data"].to_numpy() for func in (original, optimized)]
        np.testing.assert_allclose(a, b, rtol=1e-9, atol=1e-9, equal_nan=True)


def test_all_factors_cannot_see_future():
    from vnpy.alpha.dataset.utility import calculate_by_expression, register_functions
    register_functions([run.ts_mean, run.ts_std])
    df = fixture_df()
    boundary = datetime(2020, 1, 25)
    changed = df.with_columns([pl.when(pl.col("datetime") > boundary).then(pl.col(c) * 17)
                               .otherwise(pl.col(c)).alias(c) for c in ("open", "close", "volume")])
    candidates = json.loads(Path(__file__).with_name("candidates.json").read_text(encoding="utf-8"))
    for name, item in candidates.items():
        old = calculate_by_expression(df, item["expression"]).filter(pl.col("datetime") <= boundary).sort("vt_symbol", "datetime")
        new = calculate_by_expression(changed, item["expression"]).filter(pl.col("datetime") <= boundary).sort("vt_symbol", "datetime")
        np.testing.assert_allclose(old["data"].to_numpy(), new["data"].to_numpy(), atol=1e-10, equal_nan=True, err_msg=name)
