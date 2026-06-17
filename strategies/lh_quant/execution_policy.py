from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd


def macd_hist_slope_recent(hist: pd.DataFrame, n: int = 5) -> float:
    if hist is None or hist.empty or len(hist) < max(26, n + 2):
        return np.nan
    close = hist["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    h = (macd - signal).tail(n).dropna()
    if len(h) < 3:
        return np.nan
    x = np.arange(len(h))
    return float(np.polyfit(x, h.values, 1)[0])


def dynamic_decay_levels(entry_price: float, hard_stop_loss_pct: float, tp1_pct: float, tp2_pct: float) -> Dict[str, float]:
    return {
        "hard_stop_price": float(entry_price) * (1 - float(hard_stop_loss_pct)),
        "tp1_price": float(entry_price) * (1 + float(tp1_pct)),
        "tp2_price": float(entry_price) * (1 + float(tp2_pct)),
    }


def dynamic_decay_exit_signal(
    hist: pd.DataFrame,
    entry_price: float,
    tp1_pct: float,
    tp2_pct: float,
    decay_require_ma_and_macd: bool = False,
) -> Tuple[Optional[float], str]:
    """
    动态持仓日级离场信号（与 backtest dynamic_decay 规则同源）：
    - 先判 TP2、TP1；
    - 再判衰减离场（MA5走弱与/或MACD柱体斜率转负）。
    """
    if hist is None or hist.empty:
        return None, "hold"
    row = hist.iloc[-1]
    high = float(row["high"])
    close_now = float(row["close"])
    levels = dynamic_decay_levels(entry_price, hard_stop_loss_pct=0.0, tp1_pct=tp1_pct, tp2_pct=tp2_pct)
    if high >= levels["tp2_price"]:
        return levels["tp2_price"], "tp2"
    if high >= levels["tp1_price"]:
        return levels["tp1_price"], "tp1"

    if len(hist) < 30:
        return None, "hold"
    ma5 = hist["close"].rolling(5).mean()
    ma5_now = float(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else np.nan
    ma5_prev = float(ma5.iloc[-6]) if len(ma5.dropna()) >= 6 else np.nan
    ma5_slope_5d = (ma5_now / ma5_prev - 1) * 100 if pd.notna(ma5_now) and pd.notna(ma5_prev) and ma5_prev != 0 else np.nan
    macd_slope = macd_hist_slope_recent(hist, n=5)
    weak_ma = pd.notna(ma5_now) and pd.notna(ma5_slope_5d) and close_now < ma5_now and ma5_slope_5d <= 0
    weak_macd = pd.notna(macd_slope) and macd_slope < 0
    if decay_require_ma_and_macd:
        if weak_ma and weak_macd:
            return close_now, "decay"
    elif weak_ma or weak_macd:
        return close_now, "decay"
    return None, "hold"


def dynamic_decay_plan_texts(
    entry_type: str,
    entry_price: float,
    hard_stop_loss_pct: float,
    tp1_pct: float,
    tp2_pct: float,
    max_hold_days: int,
) -> Dict[str, str]:
    levels = dynamic_decay_levels(entry_price, hard_stop_loss_pct, tp1_pct, tp2_pct)
    if entry_type == "cross_flow":
        build = f"首仓50%，站稳触发价后补仓50%；基准价{entry_price:.2f}"
    elif entry_type == "breakout":
        build = f"突破确认分两笔建仓(50%+50%)；基准价{entry_price:.2f}"
    elif entry_type == "setup":
        build = f"埋伏试仓40%，确认后加仓60%；基准价{entry_price:.2f}"
    else:
        build = "无明确信号，不建仓"
    hold = f"按日执行dynamic_decay：先看TP，再看MA5/MACD衰减；最长持有{int(max_hold_days)}日"
    tp = f"止盈一档{levels['tp1_price']:.2f}（减仓），二档{levels['tp2_price']:.2f}（继续止盈）"
    sl = f"硬止损{levels['hard_stop_price']:.2f}；若出现衰减信号可提前离场"
    return {
        "build_plan": build,
        "hold_plan": hold,
        "take_profit_plan": tp,
        "stop_loss_plan": sl,
    }
