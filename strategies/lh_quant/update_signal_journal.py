#!/usr/bin/env python3
"""
维护信号日志（不管是否买入，都记录并统计胜率）。
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd

from searchv1 import DataFetcher


JOURNAL_COLS = [
    "signal_date",
    "ts_code",
    "name",
    "entry_type",
    "selection_tier",
    "is_trade_signal",
    "total_score",
    "latest_close",
    "wr_trend_score",
    "mf_net_rate_sum",
    "concept_rotation_score",
    "risk_hint",
    "buy_flag",
    "buy_price",
    "buy_qty",
    "sell_price",
    "sell_date",
    "sell_qty",
    "manual_note",
    "ret_5d",
    "is_win_5d",
    "ret_10d",
    "is_win_10d",
    "updated_at",
]

MANUAL_COLS = [
    "signal_date",
    "ts_code",
    "name",
    "buy_flag",
    "buy_price",
    "buy_qty",
    "sell_price",
    "sell_date",
    "sell_qty",
    "manual_note",
]


def parse_args():
    p = argparse.ArgumentParser(description="更新信号日志并回填5/10交易日表现")
    p.add_argument("--input", required=True, help="daily_selection csv")
    p.add_argument("--journal", default="output/journal/signal_journal.csv", help="signal journal path")
    p.add_argument("--manual", default="output/journal/manual_trade_inputs.csv", help="manual trade input csv path")
    p.add_argument("--top-k", type=int, default=3, help="记录前k只")
    return p.parse_args()


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in JOURNAL_COLS:
        if c not in out.columns:
            out[c] = pd.NA
    return out[JOURNAL_COLS]


def make_today_rows(daily: pd.DataFrame, top_k: int) -> pd.DataFrame:
    x = daily.head(top_k).copy()
    x["signal_date"] = pd.to_datetime(x["latest_trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    x["ret_5d"] = pd.NA
    x["is_win_5d"] = pd.NA
    x["ret_10d"] = pd.NA
    x["is_win_10d"] = pd.NA
    x["buy_flag"] = pd.NA
    x["buy_price"] = pd.NA
    x["buy_qty"] = pd.NA
    x["sell_price"] = pd.NA
    x["sell_date"] = pd.NA
    x["sell_qty"] = pd.NA
    x["manual_note"] = pd.NA
    x["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    keep = [
        "signal_date",
        "ts_code",
        "name",
        "entry_type",
        "selection_tier",
        "is_trade_signal",
        "total_score",
        "latest_close",
        "wr_trend_score",
        "mf_net_rate_sum",
        "concept_rotation_score",
        "risk_hint",
        "buy_flag",
        "buy_price",
        "buy_qty",
        "sell_price",
        "sell_date",
        "sell_qty",
        "manual_note",
        "ret_5d",
        "is_win_5d",
        "ret_10d",
        "is_win_10d",
        "updated_at",
    ]
    for c in keep:
        if c not in x.columns:
            x[c] = pd.NA
    # 英文导出里有 bool / TrueFalse，统一成可 CSV 序列化的标量
    if "is_trade_signal" in x.columns:
        x["is_trade_signal"] = x["is_trade_signal"].map(
            lambda v: int(bool(v)) if pd.notna(v) and str(v) not in ("", "nan") else pd.NA
        )
    return x[keep]


def merge_manual_inputs(journal: pd.DataFrame, manual_path: Path, today_rows: pd.DataFrame) -> pd.DataFrame:
    if manual_path.exists():
        manual = pd.read_csv(manual_path)
    else:
        manual = pd.DataFrame(columns=MANUAL_COLS)

    for c in MANUAL_COLS:
        if c not in manual.columns:
            manual[c] = pd.NA
    manual = manual[MANUAL_COLS]

    # 确保今日TopK在可填写清单中
    if not today_rows.empty:
        keys = set((str(r["signal_date"]), str(r["ts_code"])) for _, r in manual.iterrows())
        adds = []
        for _, r in today_rows.iterrows():
            k = (str(r["signal_date"]), str(r["ts_code"]))
            if k in keys:
                continue
            adds.append(
                {
                    "signal_date": r["signal_date"],
                    "ts_code": r["ts_code"],
                    "name": r["name"],
                    "buy_flag": pd.NA,
                    "buy_price": pd.NA,
                    "buy_qty": pd.NA,
                    "sell_price": pd.NA,
                    "sell_date": pd.NA,
                    "sell_qty": pd.NA,
                    "manual_note": pd.NA,
                }
            )
        if adds:
            manual = pd.concat([manual, pd.DataFrame(adds)], ignore_index=True)

    # 回写模板文件，供手填
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    manual.to_csv(manual_path, index=False, encoding="utf-8-sig")

    # 合并手填字段到journal
    mm = manual.copy()
    mm["signal_date"] = mm["signal_date"].astype(str)
    mm["ts_code"] = mm["ts_code"].astype(str)
    jj = journal.copy()
    jj["signal_date"] = jj["signal_date"].astype(str)
    jj["ts_code"] = jj["ts_code"].astype(str)
    merged = jj.merge(
        mm[
            [
                "signal_date",
                "ts_code",
                "buy_flag",
                "buy_price",
                "buy_qty",
                "sell_price",
                "sell_date",
                "sell_qty",
                "manual_note",
            ]
        ],
        on=["signal_date", "ts_code"],
        how="left",
        suffixes=("", "_m"),
    )
    for c in ("buy_flag", "buy_price", "buy_qty", "sell_price", "sell_date", "sell_qty", "manual_note"):
        merged[c] = merged[f"{c}_m"].combine_first(merged[c])
        merged = merged.drop(columns=[f"{c}_m"])
    return merged


def calc_forward_return(df: pd.DataFrame, signal_date: str, horizon: int) -> float | None:
    if df.empty:
        return None
    d = df.sort_index()
    sdt = pd.Timestamp(signal_date)
    if sdt not in d.index:
        return None
    idx = d.index.get_loc(sdt)
    if isinstance(idx, slice):
        idx = idx.start
    target_idx = idx + horizon
    if target_idx >= len(d):
        return None
    entry = float(d.iloc[idx]["close"])
    exitp = float(d.iloc[target_idx]["close"])
    if entry <= 0:
        return None
    return (exitp / entry - 1) * 100


def update_outcomes(journal: pd.DataFrame, token: str) -> pd.DataFrame:
    pending = journal[
        journal["ret_5d"].isna() | journal["ret_10d"].isna()
    ].copy()
    if pending.empty:
        return journal
    if not token:
        return journal

    fetcher = DataFetcher(token=token)
    today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))

    code_dates: Dict[str, List[pd.Timestamp]] = {}
    for _, r in pending.iterrows():
        code = str(r["ts_code"])
        sdt = pd.to_datetime(r["signal_date"], errors="coerce")
        if pd.isna(sdt):
            continue
        code_dates.setdefault(code, []).append(sdt)

    hist_cache: Dict[str, pd.DataFrame] = {}
    for code, dates in code_dates.items():
        start = min(dates).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        df = fetcher.get_stock_daily(code, start, end)
        hist_cache[code] = df

    out = journal.copy()
    for i, row in out.iterrows():
        code = str(row["ts_code"])
        sdt = str(row["signal_date"])
        h = hist_cache.get(code, pd.DataFrame())
        if pd.notna(row.get("ret_5d")) and pd.notna(row.get("ret_10d")):
            continue
        r5 = calc_forward_return(h, sdt, 5)
        r10 = calc_forward_return(h, sdt, 10)
        if pd.isna(row.get("ret_5d")) and r5 is not None:
            out.at[i, "ret_5d"] = round(r5, 2)
            out.at[i, "is_win_5d"] = int(r5 > 0)
        if pd.isna(row.get("ret_10d")) and r10 is not None:
            out.at[i, "ret_10d"] = round(r10, 2)
            out.at[i, "is_win_10d"] = int(r10 > 0)
        out.at[i, "updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return out


def main():
    args = parse_args()
    input_path = Path(args.input)
    journal_path = Path(args.journal)
    manual_path = Path(args.manual)
    if not input_path.exists():
        raise FileNotFoundError(f"找不到输入文件: {input_path}")

    daily = pd.read_csv(input_path)
    required = ["ts_code", "name", "latest_trade_date", "latest_close", "entry_type", "total_score"]
    miss = [c for c in required if c not in daily.columns]
    if miss:
        print(f"⚠ 输入文件缺少字段({miss})，跳过信号日志更新（CSV 结构已变更）。")
        return

    today_rows = make_today_rows(daily, top_k=args.top_k)

    if journal_path.exists():
        journal = pd.read_csv(journal_path)
        journal = ensure_columns(journal)
    else:
        journal = pd.DataFrame(columns=JOURNAL_COLS)

    if not today_rows.empty:
        key_old = set((str(r["signal_date"]), str(r["ts_code"])) for _, r in journal.iterrows())
        append_rows = [
            r for _, r in today_rows.iterrows()
            if (str(r["signal_date"]), str(r["ts_code"])) not in key_old
        ]
        if append_rows:
            add_df = pd.DataFrame(append_rows)
            if journal.empty:
                journal = ensure_columns(add_df)
            else:
                journal = pd.concat([journal, add_df], ignore_index=True)

    journal = merge_manual_inputs(journal, manual_path=manual_path, today_rows=today_rows)

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    journal = update_outcomes(journal, token=token)

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal.to_csv(journal_path, index=False, encoding="utf-8-sig")

    done5 = journal["is_win_5d"].dropna()
    done10 = journal["is_win_10d"].dropna()
    win5 = float(done5.mean()) if len(done5) else 0.0
    win10 = float(done10.mean()) if len(done10) else 0.0
    print(f"信号日志已更新: {journal_path}")
    print(f"手填文件: {manual_path}")
    print(f"样本数: {len(journal)} | 5日已结算: {len(done5)} 胜率: {win5:.2%} | 10日已结算: {len(done10)} 胜率: {win10:.2%}")

    def _tier_mask(series: pd.Series, *want: str) -> pd.Series:
        s = series.astype(str).str.lower().str.strip()
        m = pd.Series(False, index=series.index)
        for w in want:
            m = m | (s == w.lower())
        return m

    if "selection_tier" in journal.columns:
        sig_m = _tier_mask(journal["selection_tier"], "signal")
        wl_m = _tier_mask(journal["selection_tier"], "watchlist")
        unk_m = ~(sig_m | wl_m) | journal["selection_tier"].isna()
        for label, m in (
            ("signal", sig_m),
            ("watchlist", wl_m),
            ("tier未知/旧数据", unk_m),
        ):
            sub = journal.loc[m, "is_win_5d"].dropna()
            sub10 = journal.loc[m, "is_win_10d"].dropna()
            w5s = float(sub.mean()) if len(sub) else float("nan")
            w10s = float(sub10.mean()) if len(sub10) else float("nan")
            print(
                f"  └ [{label}] 5日: {len(sub)}笔 胜率 {w5s:.2%} | 10日: {len(sub10)}笔 胜率 {w10s:.2%}"
            )
    elif "is_trade_signal" in journal.columns:
        for flag, label in ((1, "is_trade_signal=1"), (0, "is_trade_signal=0")):
            sub = journal.loc[journal["is_trade_signal"] == flag, "is_win_5d"].dropna()
            sub10 = journal.loc[journal["is_trade_signal"] == flag, "is_win_10d"].dropna()
            w5s = float(sub.mean()) if len(sub) else float("nan")
            w10s = float(sub10.mean()) if len(sub10) else float("nan")
            print(
                f"  └ [{label}] 5日: {len(sub)}笔 胜率 {w5s:.2%} | 10日: {len(sub10)}笔 胜率 {w10s:.2%}"
            )


if __name__ == "__main__":
    main()

