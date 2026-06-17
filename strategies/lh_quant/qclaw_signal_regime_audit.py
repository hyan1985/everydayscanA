#!/usr/bin/env python3
"""
复盘 QClaw(v13/v16) 历史信号在不同市场状态下的稳定性。

输入:
- ~/.qclaw/workspace-agent-5db131fc/v13_results_*.json
- ~/.qclaw/workspace-agent-5db131fc/v16_results_*.json

输出:
- output/analysis/qclaw/qclaw_signal_events_<stamp>.csv
- output/analysis/qclaw/qclaw_regime_summary_<stamp>.json
"""

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from searchv1 import DataFetcher


V13_POSITIVE_SIGNALS = {"买入", "轻仓", "延续(买入)", "延续(轻仓)"}
V16_POSITIVE_KEYWORDS = ("🚀", "⚡", "🎯")


@dataclass
class SignalEvent:
    strategy: str
    trade_date: pd.Timestamp
    ts_code: str
    signal: str
    strength: float


def _parse_date_from_path(path: Path) -> Optional[pd.Timestamp]:
    stem = path.stem
    date_raw = stem.split("_")[-1]
    if len(date_raw) != 8 or not date_raw.isdigit():
        return None
    return pd.to_datetime(date_raw, format="%Y%m%d")


def load_v13_events(folder: Path) -> List[SignalEvent]:
    events: List[SignalEvent] = []
    for fp in sorted(folder.glob("v13_results_*.json")):
        dt = _parse_date_from_path(fp)
        if dt is None:
            continue
        try:
            rows = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(rows, list):
            continue
        for r in rows:
            signal = str(r.get("signal", ""))
            if signal not in V13_POSITIVE_SIGNALS:
                continue
            code = str(r.get("code", "")).strip()
            if not code:
                continue
            strength = float(r.get("total", 0) or 0)
            events.append(SignalEvent("v13", dt, code, signal, strength))
    return events


def load_v16_events(folder: Path) -> List[SignalEvent]:
    events: List[SignalEvent] = []
    for fp in sorted(folder.glob("v16_results_*.json")):
        dt = _parse_date_from_path(fp)
        if dt is None:
            continue
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            continue
        for r in rows:
            signal = str(r.get("signal", ""))
            if not any(k in signal for k in V16_POSITIVE_KEYWORDS):
                continue
            code = str(r.get("code", "")).strip()
            if not code:
                continue
            strength = float(r.get("score", 0) or 0)
            events.append(SignalEvent("v16", dt, code, signal, strength))
    return events


def regime_of_date(index_df: pd.DataFrame, asof_date: pd.Timestamp) -> str:
    hist = index_df[index_df.index <= asof_date].tail(120)
    if len(hist) < 70:
        return "unknown"
    close = hist["close"].astype(float)
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    now = close.iloc[-1]
    ret20 = now / close.iloc[-21] - 1 if len(close) >= 21 and close.iloc[-21] > 0 else 0
    if now > ma20 > ma60 and ret20 >= 0.03:
        return "bull"
    if now < ma20 < ma60 and ret20 <= -0.03:
        return "bear"
    return "range"


def calc_forward_metrics(df: pd.DataFrame, signal_date: pd.Timestamp, hold_days: int = 10) -> Dict[str, float]:
    trade_window = df[df.index > signal_date]
    if trade_window.empty:
        return {}
    entry_row = trade_window.iloc[0]
    entry = float(entry_row["open"])
    if entry <= 0:
        return {}
    horizon = trade_window.head(hold_days)
    if horizon.empty:
        return {}
    close_5 = float(horizon.iloc[min(4, len(horizon) - 1)]["close"])
    close_10 = float(horizon.iloc[min(9, len(horizon) - 1)]["close"])
    low_min = float(horizon["low"].min())
    high_max = float(horizon["high"].max())
    return {
        "entry": entry,
        "ret_5d": close_5 / entry - 1.0,
        "ret_10d": close_10 / entry - 1.0,
        "mdd_10d": low_min / entry - 1.0,
        "mfe_10d": high_max / entry - 1.0,
    }


def fetch_stock_daily(fetcher: DataFetcher, code: str, start_date: str, end_date: str) -> pd.DataFrame:
    try:
        df = fetcher.get_stock_daily(code, start_date, end_date)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_index()


def summarize(events_df: pd.DataFrame) -> Dict:
    out: Dict[str, Dict] = {}
    for (strategy, regime), grp in events_df.groupby(["strategy", "regime"]):
        out.setdefault(strategy, {})
        out[strategy][regime] = {
            "count": int(len(grp)),
            "win_rate_5d": float((grp["ret_5d"] > 0).mean()),
            "win_rate_10d": float((grp["ret_10d"] > 0).mean()),
            "ret_5d_mean": float(grp["ret_5d"].mean()),
            "ret_10d_mean": float(grp["ret_10d"].mean()),
            "ret_5d_median": float(grp["ret_5d"].median()),
            "ret_10d_median": float(grp["ret_10d"].median()),
            "mdd_10d_mean": float(grp["mdd_10d"].mean()),
            "mdd_10d_p25": float(grp["mdd_10d"].quantile(0.25)),
        }
    return out


def parse_args():
    p = argparse.ArgumentParser(description="QClaw v13/v16 分行情段信号复盘")
    p.add_argument("--token", type=str, default="", help="Tushare token；空则读取 TUSHARE_TOKEN")
    p.add_argument("--qclaw-dir", type=str, default="/Users/hyan/.qclaw/workspace-agent-5db131fc")
    p.add_argument("--hold-days", type=int, default=10)
    p.add_argument("--out-dir", type=str, default="output/analysis/qclaw")
    return p.parse_args()


def main():
    args = parse_args()
    token = (args.token or os.getenv("TUSHARE_TOKEN", "")).strip()
    if not token:
        raise ValueError("请设置 TUSHARE_TOKEN 或通过 --token 传入。")

    qclaw_dir = Path(args.qclaw_dir)
    v13_events = load_v13_events(qclaw_dir)
    v16_events = load_v16_events(qclaw_dir)
    events = v13_events + v16_events
    if not events:
        raise ValueError("未读取到任何 v13/v16 信号文件。")

    ev_df = pd.DataFrame([e.__dict__ for e in events]).drop_duplicates(
        subset=["strategy", "trade_date", "ts_code"], keep="first"
    )
    start = ev_df["trade_date"].min() - timedelta(days=180)
    end = ev_df["trade_date"].max() + timedelta(days=40)
    start_str, end_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    fetcher = DataFetcher(token)
    index_df = fetcher.get_index_daily("000001.SH", start_str, end_str)

    stock_cache: Dict[str, pd.DataFrame] = {}
    rows = []
    for rec in ev_df.itertuples(index=False):
        code = str(rec.ts_code)
        if code not in stock_cache:
            stock_cache[code] = fetch_stock_daily(fetcher, code, start_str, end_str)
        sdf = stock_cache[code]
        if sdf.empty:
            continue
        m = calc_forward_metrics(sdf, rec.trade_date, hold_days=args.hold_days)
        if not m:
            continue
        rows.append(
            {
                "strategy": rec.strategy,
                "trade_date": rec.trade_date.strftime("%Y-%m-%d"),
                "ts_code": code,
                "signal": rec.signal,
                "strength": float(rec.strength),
                "regime": regime_of_date(index_df, rec.trade_date),
                **m,
            }
        )

    if not rows:
        raise ValueError("信号已读取，但未能计算前瞻收益（请检查交易日覆盖）。")

    out_df = pd.DataFrame(rows).sort_values(["trade_date", "strategy", "ts_code"]).reset_index(drop=True)
    summary = summarize(out_df)
    summary["meta"] = {
        "event_count": int(len(out_df)),
        "date_range": [str(out_df["trade_date"].min()), str(out_df["trade_date"].max())],
        "hold_days": int(args.hold_days),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    events_path = out_dir / f"qclaw_signal_events_{stamp}.csv"
    summary_path = out_dir / f"qclaw_regime_summary_{stamp}.json"
    out_df.to_csv(events_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"events -> {events_path}")
    print(f"summary -> {summary_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
