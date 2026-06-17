#!/usr/bin/env python3
"""
上涨节点前特征分析（基于本地历史缓存）

目标：
1) 从历史中标注“上涨节点”样本
2) 统计节点前关键因子的有效区间
3) 给出参数建议范围（用于减少盲调）
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd


@dataclass
class LabelSpec:
    horizon: int
    min_upside: float
    max_drawdown: float


def wr_series(df: pd.DataFrame, period: int) -> pd.Series:
    hh = df["high"].rolling(period).max()
    ll = df["low"].rolling(period).min()
    x = (hh - df["close"]) / (hh - ll) * 100
    return x.replace([np.inf, -np.inf], np.nan)


def wr_trend_score(df: pd.DataFrame) -> pd.Series:
    wr_fast = wr_series(df, 14)
    wr_slow = wr_series(df, 28)
    down_days = (wr_fast.diff() < 0).rolling(3).sum()
    level = ((100 - wr_fast) / 80 * 100).clip(0, 100)
    rel = (50 + (wr_slow - wr_fast) * 1.5).clip(0, 100)
    down = (down_days / 3 * 100).clip(0, 100)
    return (level * 0.4 + rel * 0.3 + down * 0.3).replace([np.inf, -np.inf], np.nan)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().sort_values("trade_date").reset_index(drop=True)
    c = d["close"]
    v = d["volume"]

    ma5 = c.rolling(5).mean()
    ma10 = c.rolling(10).mean()
    ma20 = c.rolling(20).mean()

    gap = ma10 - ma5
    gap_prev = gap.shift(3)
    gap_shrink_ratio = gap / gap_prev

    d["is_just_cross"] = (ma5 >= ma10) & (ma5.shift(1) < ma10.shift(1))
    d["is_pre_cross"] = (gap > 0) & (gap_shrink_ratio <= 0.7)
    d["ma5_slope_5d"] = (ma5 / ma5.shift(5) - 1) * 100
    d["runup_10d_pct"] = (c / c.shift(10) - 1) * 100
    d["ma20_slope_20d"] = (ma20 / ma20.shift(20) - 1) * 100
    d["vol_ratio_5_20"] = v.rolling(5).mean() / v.rolling(20).mean()
    d["wr_trend_score"] = wr_trend_score(d)
    d["wr_fast"] = wr_series(d, 14)
    d["wr_down_days"] = (d["wr_fast"].diff() < 0).rolling(3).sum()
    return d


def build_labels(df: pd.DataFrame, spec: LabelSpec) -> pd.Series:
    c = df["close"]
    future_max = c.shift(-1).rolling(spec.horizon).max().shift(-(spec.horizon - 1))
    future_min = c.shift(-1).rolling(spec.horizon).min().shift(-(spec.horizon - 1))
    upside = future_max / c - 1
    drawdown = future_min / c - 1
    return (upside >= spec.min_upside) & (drawdown >= spec.max_drawdown)


def summarize_ranges(samples: pd.DataFrame, label_col: str, feature_cols: List[str]) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    pos = samples[samples[label_col] == 1]
    neg = samples[samples[label_col] == 0]
    for col in feature_cols:
        p = pd.to_numeric(pos[col], errors="coerce").dropna()
        n = pd.to_numeric(neg[col], errors="coerce").dropna()
        if len(p) < 30 or len(n) < 30:
            continue
        out[col] = {
            "pos_p25": float(p.quantile(0.25)),
            "pos_p50": float(p.quantile(0.5)),
            "pos_p75": float(p.quantile(0.75)),
            "neg_p50": float(n.quantile(0.5)),
            "neg_p75": float(n.quantile(0.75)),
        }
    return out


def main():
    p = argparse.ArgumentParser(description="上涨节点前特征分析")
    p.add_argument("--cache-dir", default="backtest_cache/daily")
    p.add_argument("--max-files", type=int, default=1200, help="最多分析多少只股票缓存")
    args = p.parse_args()

    cache_dir = Path(args.cache_dir)
    files = sorted(cache_dir.glob("*.csv"))
    if not files:
        raise ValueError("未找到缓存数据文件。")
    files = files[: max(1, args.max_files)]

    all_rows = []
    for i, fp in enumerate(files, 1):
        try:
            d = pd.read_csv(fp, parse_dates=["trade_date"])
        except Exception:
            continue
        if d.empty or len(d) < 260:
            continue
        d = build_features(d)
        d["label_5d"] = build_labels(d, LabelSpec(horizon=5, min_upside=0.08, max_drawdown=-0.04)).astype(int)
        d["label_10d"] = build_labels(d, LabelSpec(horizon=10, min_upside=0.12, max_drawdown=-0.06)).astype(int)
        d["ts_code"] = fp.stem.split("_")[0]
        # 候选池：只看接近你实盘触发的样本
        cands = d[(d["is_pre_cross"] | d["is_just_cross"]) & (d["ma5_slope_5d"] > 0)].copy()
        if not cands.empty:
            all_rows.append(cands)
        if i % 300 == 0:
            print(f"progress {i}/{len(files)}")

    if not all_rows:
        raise ValueError("没有形成可分析样本，请放宽条件或增加max-files。")

    sample = pd.concat(all_rows, ignore_index=True)
    feature_cols = [
        "wr_trend_score",
        "wr_fast",
        "wr_down_days",
        "ma5_slope_5d",
        "ma20_slope_20d",
        "runup_10d_pct",
        "vol_ratio_5_20",
    ]

    s5 = summarize_ranges(sample, "label_5d", feature_cols)
    s10 = summarize_ranges(sample, "label_10d", feature_cols)

    # 参数建议：优先基于5日标签（与你持仓风格更贴近）
    suggest = {}
    if "wr_trend_score" in s5:
        suggest["wr_trend_score_min"] = round(max(35.0, min(70.0, s5["wr_trend_score"]["pos_p25"])), 2)
    if "runup_10d_pct" in s5:
        suggest["max_runup_10d_pct"] = round(max(5.0, min(20.0, s5["runup_10d_pct"]["pos_p75"])), 2)
    if "ma20_slope_20d" in s5:
        suggest["min_ma20_slope_20d"] = round(max(-3.0, min(5.0, s5["ma20_slope_20d"]["pos_p25"])), 2)
    if "vol_ratio_5_20" in s5:
        suggest["vol_ratio_5_20_range"] = [
            round(float(sample["vol_ratio_5_20"].quantile(0.25)), 2),
            round(float(sample["vol_ratio_5_20"].quantile(0.75)), 2),
        ]

    out = {
        "sample_count": int(len(sample)),
        "label_5d_positive_rate": float(sample["label_5d"].mean()),
        "label_10d_positive_rate": float(sample["label_10d"].mean()),
        "ranges_5d": s5,
        "ranges_10d": s10,
        "suggested_params": suggest,
    }

    out_dir = Path("output") / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_file = out_dir / "rally_node_feature_report.json"
    md_file = out_dir / "rally_node_feature_report.md"
    json_file.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 上涨节点前特征报告",
        "",
        f"- 样本量: {out['sample_count']}",
        f"- 5日节点命中率: {out['label_5d_positive_rate']:.2%}",
        f"- 10日节点命中率: {out['label_10d_positive_rate']:.2%}",
        "",
        "## 参数建议（基于5日标签）",
    ]
    for k, v in suggest.items():
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "详细分位数见 `output/analysis/rally_node_feature_report.json`。",
    ]
    md_file.write_text("\n".join(lines), encoding="utf-8")

    print(f"saved {json_file}")
    print(f"saved {md_file}")


if __name__ == "__main__":
    main()

