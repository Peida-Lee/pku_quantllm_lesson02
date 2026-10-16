"""Read only the supplied Feather ZIP and summarize bounded metadata coverage.

No networking, no downloads, no ST truth labels, and no edits to the archive.
"""
import os
for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(key, "4")
import json
from datetime import datetime
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather

pa.set_cpu_count(4)
pa.set_io_thread_count(2)
ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[1] / "feathers.zip"
FIELDS = ("open", "high", "low", "close", "amt", "num_trades", "cumadj")
PERIODS = {"2025": ("2025-01-02", "2025-12-31"), "supplement_2026_09_30": ("2026-09-30", "2026-09-30")}


def quantiles(values):
    values = values[np.isfinite(values) & (values > 0)]
    return {f"p{q}": float(np.percentile(values, q)) if values.size else None for q in (25, 50, 75, 95)}


def main():
    rows, summary, arrays = [], {}, {}
    dates, symbols = None, None
    with zipfile.ZipFile(SOURCE) as archive:
        for field in FIELDS:
            member = f"feathers/{field}.feather"
            table = feather.read_table(pa.BufferReader(archive.read(member)))
            observed_dates = np.asarray(table.column("date").to_numpy()).astype("datetime64[D]")
            observed_symbols = [x for x in table.column_names if x != "date"]
            if dates is None:
                dates, symbols = observed_dates, observed_symbols
                masks = {name: (dates >= np.datetime64(start)) & (dates <= np.datetime64(end)) for name, (start, end) in PERIODS.items()}
                for name, mask in masks.items():
                    selected = dates[mask]
                    summary[name] = {"requested_start": PERIODS[name][0], "requested_end": PERIODS[name][1], "date_count": int(mask.sum()),
                                     "first_observed_date": str(selected[0]) if selected.size else None, "last_observed_date": str(selected[-1]) if selected.size else None,
                                     "symbols_in_schema": len(symbols), "expected_schema_cells": int(mask.sum()) * len(symbols)}
                    pd.DataFrame({"date": selected.astype(str)}).to_csv(ROOT / f"calendar_{name}.csv", index=False, encoding="utf-8-sig")
            if not np.array_equal(observed_dates, dates) or observed_symbols != symbols:
                raise ValueError("Matrix axes disagree: " + field)
            values = np.column_stack([table.column(name).to_numpy(zero_copy_only=False) for name in symbols])
            arrays[field] = {name: values[mask].copy() for name, mask in masks.items()}
            for name, mask in masks.items():
                data = arrays[field][name]
                finite = np.isfinite(data)
                valid = finite & (data > 0)
                daily = valid.sum(axis=1)
                rows.append({"period": name, "field": field, "dates": int(mask.sum()), "symbols": len(symbols), "schema_cells": int(data.size),
                             "finite_cells": int(finite.sum()), "nonfinite_cells": int((~finite).sum()), "positive_cells": int(valid.sum()),
                             "zero_cells": int((finite & (data == 0)).sum()), "negative_cells": int((finite & (data < 0)).sum()),
                             "symbols_with_any_positive": int(valid.any(axis=0).sum()), "positive_symbols_daily_min": int(daily.min()) if daily.size else 0,
                             "positive_symbols_daily_max": int(daily.max()) if daily.size else 0,
                             "positive_coverage_fraction": float(valid.mean()) if data.size else None})
            if field == "amt":
                amount20 = pd.DataFrame(values).rolling(20, min_periods=20).mean().shift(1).to_numpy()
                active20 = pd.DataFrame((values > 0).astype(float)).rolling(20, min_periods=20).sum().shift(1).to_numpy()
                for name, mask in masks.items():
                    lagged, active, current = amount20[mask], active20[mask], values[mask]
                    known = np.isfinite(lagged)
                    liquid = known & (lagged >= 10_000_000) & (active >= 15)
                    prior_capacity = np.where(liquid, lagged * .01, np.nan)
                    posthoc_capacity = np.where(liquid & np.isfinite(current) & (current > 0), np.minimum(prior_capacity, current * .01), np.nan)
                    summary[name]["capacity_source"] = {
                        "source_field": "amt", "unit": "Archive metadata does not specify a currency unit; numeric amounts are reported in archive units",
                        "lagged_20day_mean_requires": "20 nonmissing observations within the preceding 20 package dates; shifted by one date",
                        "illustrative_liquidity_filter": "prior20_mean>=10000000 archive units and prior20 positive-amount dates>=15; not a verified CNY or ST rule",
                        "lagged_20day_mean_known_cells": int(known.sum()), "illustrative_liquid_cells": int(liquid.sum()),
                        "prior_1pct_capacity_quantiles": quantiles(prior_capacity), "posthoc_min_prior_1pct_and_current_1pct_quantiles": quantiles(posthoc_capacity),
                        "posthoc_only_warning": "Current day final amount is not available at the open and does not establish auction depth or opening fills"}
                    capacity_rows = []
                    for pos, date in enumerate(dates[mask]):
                        capacity_rows.append({"date": str(date), "prior20_mean_known_symbols": int(known[pos].sum()), "illustrative_liquid_symbols": int(liquid[pos].sum()),
                                              "current_positive_amount_symbols": int((np.isfinite(current[pos]) & (current[pos] > 0)).sum()),
                                              **{"prior_capacity_" + key: value for key, value in quantiles(prior_capacity[pos]).items()},
                                              **{"posthoc_capacity_" + key: value for key, value in quantiles(posthoc_capacity[pos]).items()}})
                    pd.DataFrame(capacity_rows).to_csv(ROOT / f"capacity_coverage_{name}.csv", index=False, encoding="utf-8-sig")
            del values, table
    for name in PERIODS:
        o, h, l, c = [arrays[field][name] for field in ("open", "high", "low", "close")]
        complete = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (o > 0) & (h > 0) & (l > 0) & (c > 0)
        bad_bar = complete & ((h + 1e-8 < np.maximum(o, c)) | (l - 1e-8 > np.minimum(o, c)) | (h < l))
        summary[name]["ohlc_complete_positive_cells"] = int(complete.sum())
        summary[name]["ohlc_order_violations"] = int(bad_bar.sum())
        summary[name]["close_symbols_with_any_valid_price"] = int((np.isfinite(c) & (c > 0)).any(axis=0).sum())
    pd.DataFrame(rows).to_csv(ROOT / "source_field_coverage.csv", index=False, encoding="utf-8-sig")
    artifact = {"created_at": datetime.now().astimezone().isoformat(), "source": str(SOURCE), "network_used": False, "source_modified": False,
                "source_archive_bytes": SOURCE.stat().st_size, "fields": list(FIELDS), "source_date_count": len(dates), "source_symbol_count": len(symbols),
                "source_dates_unique": len(np.unique(dates)) == len(dates), "source_dates_strictly_increasing": bool(np.all(np.diff(dates).astype(int) > 0)),
                "axes_match_all_seven_matrices": True, "exchange_suffix_counts": pd.Series([s.rsplit(".", 1)[-1] for s in symbols]).value_counts().to_dict(),
                "missing_truth_fields": ["historical_isST", "official_tradestatus", "official_limit_up", "official_limit_down", "listing_and_delisting_effective_dates", "opening_auction_volume", "bid_ask_queue", "cash_dividend_and_share_delivery_events"],
                "historical_ST_known_cells": 0, "unknown_ST_must_not_be_filled_from_current_names": True,
                "periods": summary, "transfer_hash_checks_performed": 0, "resource_limit": "Arrow CPU4 IO2; no network or parallel workers"}
    (ROOT / "package_coverage.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
