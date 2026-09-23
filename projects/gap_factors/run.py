"""按冻结方案执行 16 个跳空动量因子；使用 vnpy.alpha 的原生表达式算子。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["POLARS_MAX_THREADS"] = "4"
os.environ["OMP_NUM_THREADS"] = "4"
os.environ["OPENBLAS_NUM_THREADS"] = "4"
os.environ["MKL_NUM_THREADS"] = "4"
os.environ["MPLCONFIGDIR"] = str(ROOT / ".cache" / "matplotlib")
os.chdir(ROOT)
(ROOT / ".vntrader").mkdir(exist_ok=True)

import argparse
import hashlib
import json
import pickle
import shutil
import time
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import polars as pl
import statsmodels.api as sm
from vnpy.alpha.dataset.utility import calculate_by_expression, DataProxy, register_functions

BASE = Path(__file__).resolve().parent
CACHE = ROOT / "raw/course_materials/pku_demo/projects/alpha_mining_demo/runs/cache"
SEGMENTS = {"train": ("2008-01-01", "2016-12-31"), "valid": ("2017-01-01", "2018-12-31"),
            "historical_test": ("2019-01-01", "2020-08-31")}


def ts_mean(feature, window):
    """等价原生滚动均值；避免教材每一行回调 Python 的开销。"""
    return DataProxy(feature.df.select("datetime", "vt_symbol",
        pl.col("data").cast(pl.Float64).fill_nan(None).rolling_mean(window, min_samples=1).over("vt_symbol")))


def ts_std(feature, window):
    """与教材 np.nanstd(ddof=0) 保持一致。"""
    return DataProxy(feature.df.select("datetime", "vt_symbol",
        pl.col("data").cast(pl.Float64).fill_nan(None).rolling_std(window, min_samples=1, ddof=0).over("vt_symbol")))


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False, default=str), encoding="utf-8")


def eligible_keys(df, filters):
    """同一成分区间不能产生重复样本；T 日资格只用于 T 日。"""
    intervals = pl.DataFrame([
        {"vt_symbol": symbol, "member_start": start, "member_end": end}
        for symbol, spans in filters.items() for start, end in spans
    ])
    return (df.select("datetime", "vt_symbol").join(intervals, on="vt_symbol")
            .filter(pl.col("datetime").is_between(pl.col("member_start"), pl.col("member_end")))
            .select("datetime", "vt_symbol").unique())


def make_labels(df):
    """使用交易日历连接，不能用个股下一条记录掩盖缺失/停牌。"""
    calendar = df.select("datetime").unique().sort("datetime").with_columns(
        pl.col("datetime").shift(-1).alias("entry_date"),
        pl.col("datetime").shift(-3).alias("exit_date"))
    quotes = df.select("datetime", "vt_symbol", "close", "volume")
    labels = (df.select("datetime", "vt_symbol").join(calendar, on="datetime")
              .join(quotes.rename({"datetime": "entry_date", "close": "entry_close", "volume": "entry_volume"}),
                    on=["entry_date", "vt_symbol"], how="left")
              .join(quotes.rename({"datetime": "exit_date", "close": "exit_close", "volume": "exit_volume"}),
                    on=["exit_date", "vt_symbol"], how="left"))
    good = ((pl.col("entry_close") > 0) & (pl.col("exit_close") > 0)
            & pl.col("entry_close").is_finite() & pl.col("exit_close").is_finite()
            & (pl.col("entry_volume") > 0) & (pl.col("exit_volume") > 0))
    return labels.with_columns(pl.when(good).then(pl.col("exit_close") / pl.col("entry_close") - 1)
                               .otherwise(None).alias("label")).select(
        "datetime", "vt_symbol", "entry_date", "exit_date", "label")


def segment_panel(panel, start, end):
    return panel.filter(pl.col("datetime").is_between(datetime.fromisoformat(start), datetime.fromisoformat(end))
                        & (pl.col("exit_date") <= datetime.fromisoformat(end)))


def daily_metrics(panel):
    usable = panel.filter(pl.col("factor").is_finite() & pl.col("label").is_finite())
    daily = usable.group_by("datetime").agg(
        pl.len().alias("n"), pl.col("factor").n_unique().alias("unique_factor"),
        pl.col("label").n_unique().alias("unique_label"),
        pl.corr("factor", "label").alias("ic"),
        pl.corr("factor", "label", method="spearman").alias("rank_ic"))
    daily = daily.filter((pl.col("n") >= 30) & (pl.col("unique_factor") > 1) & (pl.col("unique_label") > 1)
                         & pl.col("ic").is_finite() & pl.col("rank_ic").is_finite()).sort("datetime")
    usable = usable.join(daily.select("datetime"), on="datetime")
    # 平均秩不按股票代码打破并列值；常量/不足十分组的截面不会伪造单调性。
    grouped = usable.with_columns((((pl.col("factor").rank("average").over("datetime") - 1)
                                    / pl.len().over("datetime") * 10).floor() + 1).cast(pl.Int32).alias("decile"))
    groups = grouped.group_by("datetime", "decile").agg(pl.col("label").mean().alias("mean_label"), pl.len().alias("n"))
    return daily.to_pandas(), groups.sort("datetime", "decile").to_pandas(), usable.height


def summarize(daily, groups, nrows):
    ric = daily["rank_ic"].to_numpy()
    ic = daily["ic"].to_numpy()
    if len(ric) < 10:
        return {"days": len(ric), "rows": nrows, "error": "有效截面不足"}
    fit = sm.OLS(ric, np.ones((len(ric), 1))).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
    q = groups.groupby("decile")["mean_label"].mean()
    return {"days": len(ric), "rows": nrows, "median_stocks": float(daily["n"].median()),
            "ic_mean": float(ic.mean()), "ic_std": float(ic.std(ddof=1)),
            "ir": float(ic.mean() / ic.std(ddof=1)),
            "rank_ic_mean": float(ric.mean()), "rank_ic_std": float(ric.std(ddof=1)),
            "rank_ir": float(ric.mean() / ric.std(ddof=1)),
            "positive_rank_ic_fraction": float((ric > 0).mean()),
            "rank_ic_hac_ci95": fit.conf_int().tolist()[0], "hac_p_two_sided": float(fit.pvalues[0]),
            "p_bonferroni_16": float(min(1, fit.pvalues[0] * 16)),
            "rank_ic_autocorr1": float(pd.Series(ric).autocorr()),
            "rank_ic_skew": float(pd.Series(ric).skew()),
            "decile_mean_label": {str(k): float(v) for k, v in q.items()},
            "q10_minus_q1_mean_label": float(q.get(10, np.nan) - q.get(1, np.nan))
                if 1 in q and 10 in q else None}


def audit_data(df, filters):
    ohlc = ["open", "high", "low", "close"]
    invalid = pl.any_horizontal([~pl.col(c).is_finite() | pl.col(c).is_null() | (pl.col(c) <= 0) for c in ohlc])
    invalid = invalid | (pl.col("high") < pl.max_horizontal("open", "close", "low")) | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
    bad = df.filter(invalid).height
    duplicates = df.height - df.unique(["datetime", "vt_symbol"]).height
    return {"rows_original": df.height, "symbols": df["vt_symbol"].n_unique(),
            "date_min": str(df["datetime"].min()), "date_max": str(df["datetime"].max()),
            "duplicate_keys": duplicates, "invalid_ohlc_rows": bad,
            "zero_or_missing_volume_rows": df.filter(pl.col("volume").is_null() | (pl.col("volume") <= 0)).height,
            "nulls": dict(zip(df.columns, df.null_count().row(0))), "filters_symbols": len(filters)}, invalid


def write_report(out, results, selected, audit, elapsed):
    lines = ["# 第二讲：第 1 轮跳空动量实验", "", f"本次实际运行：16 个因子、1 轮，用时 {elapsed:.1f} 秒。使用课程原始股票数据缓存；没有使用合成市场数据。",
             "", f"原始数据 {audit['rows_original']:,} 行 / {audit['symbols']} 只股票，日期 {audit['date_min'][:10]} 至 {audit['date_max'][:10]}。",
             f"历史成分过滤后研究区间内 {audit['eligible_research_rows']:,} 行；数据审计详见 data_audit.json。",
             "", "所有因子在 T 日收盘后形成，目标是 T+1 收盘到 T+3 收盘的收益。RankIC 是每天股票排序与后续收益排序的相关；负值反对正向动量假设。",
             "", "|因子|训练 RankIC|训练 RankIR|验证 RankIC|验证 RankIR|历史测试 RankIC|", "|---|---:|---:|---:|---:|---:|"]
    for name, item in results.items():
        t, v, h = [item[seg] for seg in SEGMENTS]
        lines.append(f"|{name}|{t['rank_ic_mean']:.4f}|{t['rank_ir']:.3f}|{v['rank_ic_mean']:.4f}|{v['rank_ir']:.3f}|{h['rank_ic_mean']:.4f}|")
    confirmed = [name for name in selected if results[name]["valid"]["rank_ic_mean"] > 0
                 and results[name]["valid"]["p_bonferroni_16"] < .05 and results[name]["valid"]["days"] >= 60]
    lines += ["", f"训练期按事前规则入选：{', '.join(selected) or '无'}。验证达标：{', '.join(confirmed) or '无'}。",
              "", "全部候选与公式见 candidates.json；完整统计、HAC 95% 区间、16 次比较校正见 metrics.json；每个因子的日度相关与十分组收益表均单独留存。",
              "", "![训练与验证相关](rank_ic_comparison.png)", "", "本轮未把负相关因子翻转成正收益策略，也未据验证结果继续调参数。完成一轮后按方案停止。",
              "", "解释范围：这是复用教学历史数据的因子实验，不是新独立样本验证，也不是可执行的交易策略收益。老师此前已观察这些年份。数据缓存的复权、ST、涨跌停、退市和融券条件未获独立核验。",
              "", "计算修正：使用成分过滤后的 T 日股票池；标签以全市场交易日历连接，缺失端点排除；每段末尾剔除跨段标签。原教材评估读取 result_df 的做法绕过了 raw_df 上的成分筛选，因此数值不应强求与原报告一致。",
              "", "两日重叠收益没有逐日复利、没有伪装成策略年化；十分组仅用于观察预测关系。HAC 滞后 5 日用于处理重叠与短期序列相关，不消除所有历史选择偏差。"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return confirmed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    start_time = time.perf_counter()
    candidates = json.loads((BASE / "candidates.json").read_text(encoding="utf-8"))
    register_functions([ts_mean, ts_std])
    assert 10 <= len(candidates) <= 20
    shutil.copyfile(BASE / "candidates.json", out / "candidates.json")
    shutil.copyfile(BASE / "protocol.md", out / "protocol.md")
    write_json(out / "input_manifest.json", {str(p.relative_to(ROOT)): sha256(p) for p in [BASE / "candidates.json", BASE / "protocol.md", Path(__file__), CACHE / "base_dataset.pkl", CACHE / "filters.pkl"]})
    with (CACHE / "base_dataset.pkl").open("rb") as f:
        dataset = pickle.load(f)  # 仅加载已审阅的课程包，绝不接受网上任意 pickle。
    with (CACHE / "filters.pkl").open("rb") as f:
        filters = pickle.load(f)
    audit, invalid = audit_data(dataset.df, filters)
    if audit["duplicate_keys"]:
        raise ValueError("数据键重复，不能直接计算")
    # 仅使用截至测试期末的行情；保留学习窗口之前的历史。
    df = dataset.df.filter(pl.col("datetime") <= datetime(2020, 8, 31)).sort("vt_symbol", "datetime")
    # 不删除坏行后再 shift：置空价格，保持每只股票原始时间序列的行位置。
    df = df.with_columns([pl.when(invalid).then(None).otherwise(pl.col(c)).alias(c) for c in ["open", "high", "low", "close"]])
    labels = make_labels(df)
    keys = eligible_keys(df, filters)
    keys = keys.join(df.filter(pl.col("volume") > 0).select("datetime", "vt_symbol"), on=["datetime", "vt_symbol"])
    research_keys = keys.filter(pl.col("datetime") >= datetime(2008, 1, 1))
    audit["eligible_research_rows"] = research_keys.height
    audit["eligible_daily_stock_counts"] = {k: float(v) for k, v in zip(["min", "median", "max"],
        research_keys.group_by("datetime").len().select(pl.col("len").min().alias("min"), pl.col("len").median().alias("median"), pl.col("len").max().alias("max")).row(0))}
    write_json(out / "data_audit.json", audit)
    results = {}
    # 第一次仅计算训练期统计并冻结选择，随后才计算验证/历史测试统计。
    for name, spec in candidates.items():
        print(f"计算 {name}: {spec['expression']}", flush=True)
        factor = calculate_by_expression(df, spec["expression"]).rename({"data": "factor"})
        panel = factor.join(keys, on=["datetime", "vt_symbol"]).join(labels, on=["datetime", "vt_symbol"])
        panel.write_parquet(out / f"{name}_panel.parquet")
        sliced = segment_panel(panel, *SEGMENTS["train"])
        daily, groups, rows = daily_metrics(sliced)
        daily.to_csv(out / f"{name}_train_daily.csv", index=False)
        groups.to_csv(out / f"{name}_train_deciles.csv", index=False)
        results[name] = {"expression": spec["expression"], "train": summarize(daily, groups, rows)}
    selected = sorted([n for n, r in results.items() if r["train"].get("rank_ic_mean", -1) >= .01
                       and r["train"].get("rank_ir", -1) >= .05], key=lambda n: (-results[n]["train"]["rank_ir"], n))[:3]
    write_json(out / "selection_frozen_before_validation.json", {"selected": selected, "selection_source": "train only", "created_at": datetime.now().astimezone().isoformat()})
    for name in candidates:
        panel = pl.read_parquet(out / f"{name}_panel.parquet")
        for seg in ("valid", "historical_test"):
            sliced = segment_panel(panel, *SEGMENTS[seg])
            daily, groups, rows = daily_metrics(sliced)
            daily.to_csv(out / f"{name}_{seg}_daily.csv", index=False)
            groups.to_csv(out / f"{name}_{seg}_deciles.csv", index=False)
            results[name][seg] = summarize(daily, groups, rows)
        print(name, {seg: round(results[name][seg]["rank_ic_mean"], 5) for seg in SEGMENTS}, flush=True)
    write_json(out / "metrics.json", results)
    names = list(candidates)
    fig, ax = plt.subplots(figsize=(11, 7))
    y = np.arange(len(names))
    for offset, seg, color in [(-.18, "train", "#36688d"), (.18, "valid", "#f49f3a")]:
        means = [results[n][seg]["rank_ic_mean"] for n in names]
        errors = [results[n][seg]["rank_ic_hac_ci95"][1] - results[n][seg]["rank_ic_mean"] for n in names]
        ax.barh(y + offset, means, height=.34, label=seg, color=color, xerr=errors, capsize=2)
    ax.set_yticks(y, names)
    ax.invert_yaxis()
    ax.axvline(0, color="#555555", linewidth=.8)
    ax.set_xlabel("Mean daily RankIC (95% HAC intervals, lag 5)")
    ax.set_title("Gap momentum: 16 fixed candidates, one round")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "rank_ic_comparison.png", dpi=150)
    plt.close(fig)
    elapsed = time.perf_counter() - start_time
    confirmed = write_report(out, results, selected, audit, elapsed)
    write_json(out / "run_summary.json", {"status": "COMPLETE", "rounds": 1, "factors": len(candidates),
               "selected": selected, "validation_confirmed": confirmed, "elapsed_seconds": elapsed, "threads": 4,
               "new_independent_test": False})
    print(f"完成：{out}; 耗时 {elapsed:.1f} 秒", flush=True)


if __name__ == "__main__":
    main()
