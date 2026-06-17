#!/usr/bin/env python3
"""
QClaw 规则历史重放（技术面近似版）

目标：
1) 用本地日线缓存重放 v13/v16 核心技术逻辑；
2) 按分段行情统计 5/10 日胜率、收益与回撤。

说明：
- 为确保可复现与可执行，本脚本仅使用价格/成交量技术因子，
  不依赖历史财务快照（PE/ROE）与当日资金流接口。
"""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class Segment:
    name: str
    start: str
    end: str


DEFAULT_SEGMENTS = [
    Segment("range_2024Q4_2025Q1", "2024-10-01", "2025-03-31"),
    Segment("bull_2025Q2_Q3", "2025-04-01", "2025-08-31"),
    Segment("drawdown_2025Q4_2026Q1", "2025-09-01", "2026-01-31"),
]


def code_to_tscode(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def calc_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.inf)
    return 100 - 100 / (1 + rs)


def calc_macd_hist(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


def calc_boll_pos(closes: pd.Series, period: int = 20, k: float = 2.0) -> pd.Series:
    ma = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = ma + k * std
    lower = ma - k * std
    band = (upper - lower).replace(0, np.nan)
    return (closes - lower) / band


def detect_consolidation(sub: pd.DataFrame, min_days: int = 25, max_days: int = 60, max_amp: float = 0.12) -> Tuple[bool, float, float, int]:
    if len(sub) < min_days:
        return False, np.nan, np.nan, 0
    highs = sub["high"].astype(float).values
    lows = sub["low"].astype(float).values
    best = (False, np.nan, np.nan, 0)
    upper = min(max_days, len(sub))
    for span in range(min_days, upper + 1):
        hh = highs[-span:].max()
        ll = lows[-span:].min()
        if ll <= 0:
            continue
        amp = (hh - ll) / ll
        if amp <= max_amp:
            best = (True, float(hh), float(ll), span)
    return best


def check_breakout(sub: pd.DataFrame, consol_high: float, consol_low: float, vol_break_mult: float = 1.5) -> Tuple[bool, int]:
    if len(sub) < 2:
        return False, 0
    vol_col = "vol" if "vol" in sub.columns else "volume"
    today = sub.iloc[-1]
    yesterday = sub.iloc[-2]
    avg_vol_5 = float(sub[vol_col].iloc[-6:-1].mean()) if len(sub) >= 6 else float(today[vol_col])
    vol_ratio = float(today[vol_col]) / avg_vol_5 if avg_vol_5 > 0 else 1.0
    is_volume_break = vol_ratio >= vol_break_mult
    price_above_high = float(today["close"]) > consol_high
    price_at_high = float(today["close"]) >= consol_high * 0.98
    is_pullback = consol_low <= float(yesterday["close"]) <= consol_high
    if price_above_high and is_volume_break and is_pullback:
        return True, 95
    if price_above_high and is_volume_break:
        return True, 85
    if price_at_high and is_volume_break:
        return True, 70
    if price_above_high:
        return True, 60
    if price_at_high:
        return True, 45
    return False, 0


def load_pool_codes(pool_path: Path) -> List[str]:
    data = json.loads(pool_path.read_text(encoding="utf-8"))
    return [code_to_tscode(k) for k in data.keys()]


def pick_cache_files(cache_dir: Path) -> Dict[str, Path]:
    best: Dict[str, Tuple[int, Path]] = {}
    for fp in cache_dir.glob("*.csv"):
        name = fp.stem
        parts = name.split("_")
        if len(parts) < 3:
            continue
        code, start, end = parts[0], parts[1], parts[2]
        score = int(start.replace("-", "")) * -1 + int(end.replace("-", ""))
        old = best.get(code)
        if old is None or score > old[0]:
            best[code] = (score, fp)
    return {k: v for k, (_, v) in best.items()}


def forward_metrics(df: pd.DataFrame, idx: int, hold_days: int = 10) -> Optional[Dict[str, float]]:
    if idx + 1 >= len(df):
        return None
    entry = float(df.iloc[idx + 1]["open"])
    if entry <= 0:
        return None
    end = min(len(df), idx + 1 + hold_days)
    horizon = df.iloc[idx + 1:end]
    if horizon.empty:
        return None
    close_5 = float(horizon.iloc[min(4, len(horizon) - 1)]["close"])
    close_10 = float(horizon.iloc[min(9, len(horizon) - 1)]["close"])
    low_min = float(horizon["low"].min())
    return {
        "ret_5d": close_5 / entry - 1.0,
        "ret_10d": close_10 / entry - 1.0,
        "mdd_10d": low_min / entry - 1.0,
    }


def in_segments(dt: pd.Timestamp, segments: List[Segment]) -> Optional[str]:
    for seg in segments:
        if pd.Timestamp(seg.start) <= dt <= pd.Timestamp(seg.end):
            return seg.name
    return None


def replay_one_code(code: str, path: Path, segments: List[Segment], step: int, hold_days: int) -> List[Dict]:
    df = pd.read_csv(path, parse_dates=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    if len(df) < 120:
        return []
    close = df["close"].astype(float)
    df["rsi14"] = calc_rsi(close)
    dif, _, hist = calc_macd_hist(close)
    df["dif"] = dif
    df["hist"] = hist
    df["hist_prev"] = df["hist"].shift(1)
    df["boll_pos"] = calc_boll_pos(close)
    df["ret_5d"] = close / close.shift(5) - 1.0
    out: List[Dict] = []

    for i in range(80, len(df) - 11, step):
        dt = pd.Timestamp(df.iloc[i]["trade_date"])
        seg_name = in_segments(dt, segments)
        if seg_name is None:
            continue

        # v13 技术近似：强否决 + 金叉确认 + 观察区降级
        rsi = float(df.iloc[i]["rsi14"]) if pd.notna(df.iloc[i]["rsi14"]) else np.nan
        rsi_prev = float(df.iloc[i - 1]["rsi14"]) if pd.notna(df.iloc[i - 1]["rsi14"]) else np.nan
        dif_now = float(df.iloc[i]["dif"])
        hist_now = float(df.iloc[i]["hist"])
        hist_prev = float(df.iloc[i]["hist_prev"]) if pd.notna(df.iloc[i]["hist_prev"]) else hist_now
        boll_pos = float(df.iloc[i]["boll_pos"]) if pd.notna(df.iloc[i]["boll_pos"]) else 0.5
        ret5 = float(df.iloc[i]["ret_5d"]) if pd.notna(df.iloc[i]["ret_5d"]) else 0.0
        veto = (
            pd.isna(rsi)
            or rsi > 88
            or rsi < 20
            or ret5 > 0.15
            or (dif_now < 0 and hist_prev > 0 and hist_now < 0)
        )
        strict_cross = (rsi_prev < 60 <= rsi) and (hist_now > hist_prev) and (boll_pos < 0.85)
        weak_cross = (rsi_prev < 60 <= rsi) and (hist_now > hist_prev)
        if not veto:
            if strict_cross:
                m = forward_metrics(df, i, hold_days)
                if m:
                    out.append({"strategy": "v13_replay", "segment": seg_name, "date": dt.strftime("%Y-%m-%d"), "ts_code": code, "signal": "buy", **m})
            elif weak_cross or (45 <= rsi < 55 and hist_now > hist_prev and boll_pos < 0.88):
                m = forward_metrics(df, i, hold_days)
                if m:
                    out.append({"strategy": "v13_replay", "segment": seg_name, "date": dt.strftime("%Y-%m-%d"), "ts_code": code, "signal": "light", **m})

        # v16 技术近似：横盘 + 启动
        sub = df.iloc[: i + 1].tail(120)
        is_consol, ch, cl, days = detect_consolidation(sub, min_days=25, max_days=60, max_amp=0.12)
        if is_consol:
            is_break, strength = check_breakout(sub, ch, cl, vol_break_mult=1.5)
            if is_break and strength >= 50:
                m = forward_metrics(df, i, hold_days)
                if m:
                    sig = "breakout" if strength >= 85 else "ready"
                    out.append({"strategy": "v16_replay", "segment": seg_name, "date": dt.strftime("%Y-%m-%d"), "ts_code": code, "signal": sig, "consol_days": days, **m})
    return out


def summarize(df: pd.DataFrame) -> Dict:
    out: Dict[str, Dict] = {}
    for (strategy, segment), g in df.groupby(["strategy", "segment"]):
        out.setdefault(strategy, {})
        out[strategy][segment] = {
            "count": int(len(g)),
            "win_rate_5d": float((g["ret_5d"] > 0).mean()),
            "win_rate_10d": float((g["ret_10d"] > 0).mean()),
            "ret_5d_mean": float(g["ret_5d"].mean()),
            "ret_10d_mean": float(g["ret_10d"].mean()),
            "ret_10d_median": float(g["ret_10d"].median()),
            "mdd_10d_mean": float(g["mdd_10d"].mean()),
        }
    return out


def parse_args():
    p = argparse.ArgumentParser(description="QClaw v13/v16 规则分段稳定性重放")
    p.add_argument("--pool-path", type=str, default="/Users/hyan/.qclaw/workspace-agent-5db131fc/v9_pool.json")
    p.add_argument("--cache-dir", type=str, default="backtest_cache/daily")
    p.add_argument("--step", type=int, default=5, help="每隔多少个交易日评估一次信号")
    p.add_argument("--hold-days", type=int, default=10)
    p.add_argument("--max-codes", type=int, default=0, help=">0 时仅重放前N只（调试用）")
    p.add_argument("--out-dir", type=str, default="output/analysis/qclaw")
    return p.parse_args()


def main():
    args = parse_args()
    pool_codes = load_pool_codes(Path(args.pool_path))
    cache_map = pick_cache_files(Path(args.cache_dir))
    universe = [c for c in pool_codes if c in cache_map]
    if args.max_codes > 0:
        universe = universe[: args.max_codes]
    if not universe:
        raise ValueError("无可用缓存代码，请先准备 backtest_cache/daily 数据。")

    rows: List[Dict] = []
    for idx, code in enumerate(universe, 1):
        rows.extend(replay_one_code(code, cache_map[code], DEFAULT_SEGMENTS, args.step, args.hold_days))
        if idx % 200 == 0:
            print(f"progress {idx}/{len(universe)}")

    if not rows:
        raise ValueError("未生成任何重放信号，请放宽条件或扩大样本。")

    df = pd.DataFrame(rows).sort_values(["date", "strategy", "ts_code"]).reset_index(drop=True)
    summary = summarize(df)
    summary["meta"] = {
        "events": int(len(df)),
        "codes": int(len(universe)),
        "step": int(args.step),
        "hold_days": int(args.hold_days),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail = out_dir / f"qclaw_replay_events_{stamp}.csv"
    summ = out_dir / f"qclaw_replay_summary_{stamp}.json"
    df.to_csv(detail, index=False, encoding="utf-8-sig")
    summ.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"detail -> {detail}")
    print(f"summary -> {summ}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
