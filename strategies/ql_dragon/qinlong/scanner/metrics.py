"""把原始字段折合成 0~10 分项。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pandas as pd


def offset_trade_calendar_days(cal_date: str, *, delta_days: int) -> str:
    dt = datetime.strptime(cal_date, "%Y%m%d") + timedelta(days=delta_days)
    return dt.strftime("%Y%m%d")


def score_theme_heat(best_concept_rank: float | None, *, from_limit: bool) -> float:
    """热门题材：热榜名次越小越强；叠加涨停当日加成。"""
    s = 0.0
    if best_concept_rank is not None and not (
        isinstance(best_concept_rank, float) and math.isnan(best_concept_rank)
    ):
        r = float(best_concept_rank)
        s = max(0.0, 10.0 - (r - 1.0) * 0.45)
    if from_limit:
        s += 1.5
    return float(min(10.0, s))


def score_news_proxy(
    hm_net: float | None,
    *,
    hm_abs_max: float,
    mf_net: float | None,
    mf_abs_max: float,
) -> float:
    """
    「新闻热度」代理：龙虎榜净额 + 东财当日大单净流入。

    hm_abs_max / mf_abs_max：同一批候选内的绝对值最大值，用于横向归一化。
    修复逻辑：仅对净流入予以奖励，净流出不加基础分。
    """
    s = 0.0
    if hm_net is not None and hm_abs_max > 1e-6:
        if hm_net > 0:
            s += min(6.0, 6.0 * min(1.0, hm_net / hm_abs_max))
            s += 1.0
        else:
            # 净流出不加基础分
            s += 0.0
    if mf_net is not None and mf_abs_max > 1e-6:
        if mf_net > 0:
            s += min(3.0, 3.0 * min(1.0, mf_net / mf_abs_max))
            s += 1.0
        else:
            s += 0.0
    return float(min(10.0, max(0.0, s)))


def score_technical_from_daily(daily: pd.DataFrame) -> tuple[float, dict[str, float]]:
    """基于日线近似「前高突破」：最近 250 根已走完 K 的最高价 vs 最新收盘；量能对比 5 日均量；新增：连板特征。"""
    if daily is None or daily.empty or len(daily) < 30:
        return 0.0, {}

    df = daily.sort_values("trade_date").reset_index(drop=True)
    last = df.iloc[-1]
    hist = df.iloc[:-1]
    window = hist.tail(250)
    if window.empty:
        return 0.0, {}

    prev_high = float(window["high"].max())
    close = float(last["close"])
    vol = float(last["vol"])
    vol_ma5 = float(window.tail(5)["vol"].mean()) if len(window) >= 5 else float("nan")

    ratio = close / prev_high if prev_high > 0 else 0.0
    vol_ratio = vol / vol_ma5 if vol_ma5 and vol_ma5 > 0 else float("nan")

    s = 0.0
    if ratio >= 1.03:
        s += 3.0
    elif ratio >= 1.01:
        s += 2.0
    elif ratio >= 0.98:
        s += 1.0
    else:
        s += max(0.0, min(1.0, (ratio - 0.90) * 10.0))

    if not math.isnan(vol_ratio):
        if vol_ratio >= 2.0:
            s += 2.0
        elif vol_ratio >= 1.5:
            s += 1.5
        elif vol_ratio >= 1.2:
            s += 1.0
        elif vol_ratio >= 1.0:
            s += 0.5

    limit_up_days = 0
    recent_10 = df.tail(10).iloc[::-1]
    for _, row in recent_10.iterrows():
        pct_chg = float(row.get("pct_chg", 0.0))
        c = float(row["close"])
        h = float(row["high"])
        if pct_chg >= 9.5 and c >= h * 0.995:
            limit_up_days += 1
        else:
            break
            
    if limit_up_days >= 3:
        s += 5.0
    elif limit_up_days == 2:
        s += 4.0
    elif limit_up_days == 1:
        s += 2.0

    dbg = {"close_to_prev_high": ratio, "vol_to_ma5": vol_ratio, "limit_up_days": limit_up_days, "close": close}
    return float(min(10.0, s)), dbg


def score_technical_from_factor(row: pd.Series) -> tuple[float, dict[str, float]]:
    """stk_factor_pro：均线多头 + MACD + KDJ 超买过滤。"""
    s = 0.0
    dbg: dict[str, float] = {}
    try:
        e5 = float(row["ema_qfq_5"])
        dbg["ema_5"] = e5
        e10 = float(row["ema_qfq_10"])
        e20 = float(row["ema_qfq_20"])
        e60 = float(row["ema_qfq_60"])
        if e5 > e10 > e20 > e60:
            s += 4.0
            dbg["ema_bull"] = 1.0
        dif = float(row["macd_dif_qfq"])
        dea = float(row["macd_dea_qfq"])
        if dif > dea and float(row.get("macd_qfq", 0.0)) > 0:
            s += 3.0
            dbg["macd_ok"] = 1.0
        k = float(row["kdj_k_qfq"])
        d = float(row["kdj_d_qfq"])
        if k >= 85 and d >= 85:
            s += 2.5
            dbg["kdj_passivated"] = 1.0
        else:
            s += 1.5
        dbg["kdj_k"] = k
    except Exception:
        pass
    return float(min(10.0, max(0.0, s))), dbg


def score_fundamental_row(row: pd.Series) -> float:
    """growth：营收/扣非利润增速 + ROE 粗糙分层。"""
    try:
        rev = row.get("revenue_yoy")
        prof = row.get("profit_dedt_yoy")
        roe = row.get("roe")
        pts = 4.0
        if rev is not None and not pd.isna(rev) and float(rev) > 20:
            pts += 2.5
        if prof is not None and not pd.isna(prof) and float(prof) > 20:
            pts += 2.5
        if roe is not None and not pd.isna(roe) and float(roe) > 10:
            pts += 1.0
        return float(min(10.0, pts))
    except Exception:
        return 4.0


def score_market_cap_and_turnover(circ_mv: float | None, turnover_rate: float | None) -> tuple[float, dict[str, float]]:
    """
    流通市值和换手率打分。
    流通市值 (单位: 万元)：偏好 20亿 - 100亿 (即 200,000 - 1,000,000 万元) 中小盘。
    换手率 (%)：偏好 10% - 40% 的高换手活跃接力区间。
    """
    s = 0.0
    dbg: dict[str, float] = {}

    if circ_mv is not None and not math.isnan(circ_mv):
        mv_yi = circ_mv / 10000.0  # 转换为亿元
        dbg["circ_mv_yi"] = mv_yi
        if 20 <= mv_yi <= 100:
            s += 5.0
        elif 100 < mv_yi <= 300 or 10 <= mv_yi < 20:
            s += 3.0
        elif 300 < mv_yi <= 500:
            s += 1.0

    if turnover_rate is not None and not math.isnan(turnover_rate):
        dbg["turnover_rate"] = turnover_rate
        if 10 <= turnover_rate <= 40:
            s += 5.0
        elif 5 <= turnover_rate < 10:
            s += 3.0
        elif 40 < turnover_rate <= 60:
            s += 2.0
        elif turnover_rate < 3:
            s += 0.0
    else:
        s += 2.0

    return float(min(10.0, s)), dbg


def score_chip_winner_rate(winner_rate: float | None) -> float:
    """筹码获利盘：中段更易呈现「合力上攻」，极端偏高更像情绪末端。"""
    if winner_rate is None or (isinstance(winner_rate, float) and math.isnan(winner_rate)):
        return 5.0
    wr = float(winner_rate)
    if 48.0 <= wr <= 80.0:
        return 8.5
    if 35.0 <= wr < 48.0:
        return 6.5
    if 80.0 < wr <= 92.0:
        return 6.0
    return 4.0


def composite_score(parts: dict[str, float], weights: dict[str, float]) -> float:
    total_w = sum(weights.get(k, 0.0) for k in parts)
    if total_w <= 0:
        return 0.0
    return float(sum(parts[k] * weights.get(k, 0.0) for k in parts) / total_w)


def default_weights() -> dict[str, float]:
    return {
        "theme": 0.20,
        "news": 0.15,
        "technical": 0.25,
        "cap_turnover": 0.15,
        "fundamental": 0.10,
        "chip": 0.15,
    }


def merge_technical_scores(s_daily: float, s_factor: float, *, w_daily: float = 0.5) -> float:
    return float(s_daily * w_daily + s_factor * (1.0 - w_daily))
