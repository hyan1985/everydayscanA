"""单票深度分析：基本面摘要、走势、技术面、箱体、持仓/空仓策略文案。

本模块只接收调用方注入的 ``pro``。拉取数据时请使用 ``qinlong.tushare_client.get_pro_api()``，
以便与全项目相同的 token 规则（见 ``qinlong.secrets``）；``scripts/analyze_stock.py`` 已按此方式调用。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from qinlong.scanner import metrics
from qinlong.scanner.calendar import latest_open_trade_date


def normalize_ts_code(raw: str) -> str | None:
    s = (raw or "").strip().upper()
    if not s:
        return None
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", s):
        return s
    m = re.fullmatch(r"(\d{6})", s)
    if m:
        code = m.group(1)
        if code.startswith(("5", "6", "9")):
            return f"{code}.SH"
        if code.startswith(("0", "1", "2", "3")):
            return f"{code}.SZ"
        if code.startswith("4", "8"):
            return f"{code}.BJ"
    return None


def resolve_ts_code(pro, query: str) -> tuple[str | None, str | None]:
    """
    解析用户输入为 ts_code。
    支持：603399.SH、603399、中文简称（stock_basic 名称包含匹配，取 list_status=L 第一条）。
    """
    q = (query or "").strip()
    norm = normalize_ts_code(q)
    if norm:
        return norm, None

    throttle = None
    try:
        from qinlong.scanner.throttle import TushareThrottle

        throttle = TushareThrottle()
    except Exception:
        pass

    def _pace():
        if throttle:
            throttle.pace_before("general")

    def _mark():
        if throttle:
            throttle.mark_after("general")

    _pace()
    try:
        basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry,area")
    finally:
        _mark()
    if basic is None or basic.empty:
        return None, "stock_basic 无数据"
    hit = basic[basic["name"].str.contains(q, na=False)]
    if hit.empty:
        return None, f"未找到名称包含「{q}」的上市股票"
    row = hit.iloc[0]
    return str(row["ts_code"]), None


def _safe_float(x: Any) -> float | None:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class BoxStats:
    window: int
    top: float
    bottom: float
    mid: float
    width_pct: float
    close: float
    position_0_1: float
    dist_to_top_pct: float
    dist_to_bottom_pct: float
    regime: str  # 窄幅横盘 / 宽幅震荡 / 单边上行区 / 单边下行区
    bars_in_window: int


def analyze_box(daily: pd.DataFrame, *, window: int = 60) -> BoxStats | None:
    """用最近 window 根 K 的 high/low 构成箱体上下沿，衡量现价在箱体内的位置。"""
    if daily is None or daily.empty:
        return None
    df = daily.sort_values("trade_date").reset_index(drop=True)
    if len(df) < max(20, window // 2):
        return None
    tail = df.tail(min(window, len(df)))
    top = float(tail["high"].max())
    bottom = float(tail["low"].min())
    last = df.iloc[-1]
    close = float(last["close"])
    mid = (top + bottom) / 2.0
    width_pct = (top - bottom) / mid * 100.0 if mid > 0 else 0.0
    span = top - bottom
    pos = (close - bottom) / span if span > 1e-9 else 0.5
    pos = max(0.0, min(1.0, pos))
    dist_top = (top - close) / close * 100.0 if close > 0 else 0.0
    dist_bot = (close - bottom) / close * 100.0 if close > 0 else 0.0

    # 粗分类：用窗口首尾涨跌 + 箱体相对高度
    first_c = float(tail.iloc[0]["close"])
    trend_pct = (close - first_c) / first_c * 100.0 if first_c > 0 else 0.0
    if width_pct < 12 and abs(trend_pct) < 8:
        regime = "窄幅横盘（近似箱体）"
    elif width_pct >= 25 or abs(trend_pct) >= 15:
        regime = "宽幅震荡或趋势段（箱体仅作参考带）"
    elif trend_pct > 10:
        regime = "偏强（整体在上移，上沿可能被反复刷新）"
    elif trend_pct < -10:
        regime = "偏弱（整体下移，下沿可能被反复刷新）"
    else:
        regime = "常规定价区间"

    return BoxStats(
        window=len(tail),
        top=top,
        bottom=bottom,
        mid=mid,
        width_pct=width_pct,
        close=close,
        position_0_1=pos,
        dist_to_top_pct=dist_top,
        dist_to_bottom_pct=dist_bot,
        regime=regime,
        bars_in_window=len(tail),
    )


@dataclass
class SidewaysStats:
    """横盘检测：小涨跌幅 + 小日内振幅（相对前一日收盘价）。"""

    trailing_days: int
    trailing_start_date: str | None
    trailing_end_date: str | None
    longest_streak_days: int
    longest_streak_start_date: str | None
    longest_streak_end_date: str | None
    scan_bars: int
    max_abs_pct_chg: float
    max_amplitude_pct: float


def _row_pct_chg(df: pd.DataFrame, i: int) -> float | None:
    """第 i 根K线涨跌幅%：优先 pct_chg，否则用收盘推算。"""
    row = df.iloc[i]
    v = _safe_float(row.get("pct_chg"))
    if v is not None:
        return v
    if i < 1:
        return None
    prev = _safe_float(df.iloc[i - 1].get("close"))
    cur = _safe_float(row.get("close"))
    if prev is None or cur is None or prev <= 0:
        return None
    return (cur / prev - 1.0) * 100.0


def _row_amplitude_vs_prev_close(df: pd.DataFrame, i: int) -> float | None:
    """日内振幅% = (高-低)/前一日收盘 * 100；需 i>=1。"""
    if i < 1:
        return None
    prev_c = _safe_float(df.iloc[i - 1].get("close"))
    if prev_c is None or prev_c <= 0:
        return None
    h = _safe_float(df.iloc[i].get("high"))
    lo = _safe_float(df.iloc[i].get("low"))
    if h is None or lo is None:
        return None
    return (h - lo) / prev_c * 100.0


def _is_sideways_day(
    df: pd.DataFrame,
    i: int,
    *,
    max_abs_pct_chg: float,
    max_amplitude_pct: float,
) -> bool:
    """i>=1：同时满足涨跌幅与振幅阈值视为「横盘日」。"""
    if i < 1:
        return False
    pc = _row_pct_chg(df, i)
    amp = _row_amplitude_vs_prev_close(df, i)
    if pc is None or amp is None:
        return False
    return abs(pc) <= max_abs_pct_chg and amp <= max_amplitude_pct


def analyze_sideways(
    daily: pd.DataFrame,
    *,
    max_abs_pct_chg: float = 2.5,
    max_amplitude_pct: float = 4.0,
    scan_window: int = 120,
) -> SidewaysStats | None:
    """
    横盘天数：
    - **尾部连续**：从最后一根K向前，连续满足「|涨跌幅|≤阈值 且 振幅≤阈值」的天数。
    - **窗口内最长**：在最近 ``scan_window`` 根K内（从第2根起可算振幅），最长连续横盘段及起止日期。
    """
    if daily is None or daily.empty or len(daily) < 3:
        return None
    df = daily.sort_values("trade_date").reset_index(drop=True)
    n = len(df)
    last_i = n - 1

    trailing = 0
    i = last_i
    while i >= 1:
        if not _is_sideways_day(df, i, max_abs_pct_chg=max_abs_pct_chg, max_amplitude_pct=max_amplitude_pct):
            break
        trailing += 1
        i -= 1
    trail_start = trail_end = None
    if trailing > 0:
        trail_end = str(df.iloc[last_i]["trade_date"])
        trail_start = str(df.iloc[last_i - trailing + 1]["trade_date"])

    start_idx = max(1, n - min(scan_window, n))
    flags: list[bool] = []
    idx_map: list[int] = []
    for j in range(start_idx, n):
        flags.append(_is_sideways_day(df, j, max_abs_pct_chg=max_abs_pct_chg, max_amplitude_pct=max_amplitude_pct))
        idx_map.append(j)

    best_len = 0
    best_start_d = best_end_d = None
    run = 0
    run_start_idx: int | None = None
    for k, ok in enumerate(flags):
        j = idx_map[k]
        if ok:
            if run == 0:
                run_start_idx = j
            run += 1
        else:
            if run > 0 and run_start_idx is not None:
                if run > best_len:
                    best_len = run
                    best_start_d = str(df.iloc[run_start_idx]["trade_date"])
                    best_end_d = str(df.iloc[j - 1]["trade_date"])
            run = 0
            run_start_idx = None
    if run > 0 and run_start_idx is not None:
        j_end = idx_map[-1]
        if run > best_len:
            best_len = run
            best_start_d = str(df.iloc[run_start_idx]["trade_date"])
            best_end_d = str(df.iloc[j_end]["trade_date"])

    return SidewaysStats(
        trailing_days=trailing,
        trailing_start_date=trail_start,
        trailing_end_date=trail_end,
        longest_streak_days=best_len,
        longest_streak_start_date=best_start_d,
        longest_streak_end_date=best_end_d,
        scan_bars=n - start_idx,
        max_abs_pct_chg=max_abs_pct_chg,
        max_amplitude_pct=max_amplitude_pct,
    )


def _period_return(df: pd.DataFrame, n: int) -> float | None:
    if df is None or df.empty or len(df) < n + 1:
        return None
    sub = df.sort_values("trade_date").reset_index(drop=True)
    a = float(sub.iloc[-(n + 1)]["close"])
    b = float(sub.iloc[-1]["close"])
    if a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def _max_drawdown_pct(df: pd.DataFrame, n: int) -> float | None:
    sub = df.sort_values("trade_date").reset_index(drop=True).tail(n)
    if sub.empty or len(sub) < 5:
        return None
    closes = sub["close"].astype(float)
    peak = closes.cummax()
    dd = (closes / peak - 1.0) * 100.0
    return float(dd.min())


def _volatility_pct_chg(df: pd.DataFrame, n: int) -> float | None:
    sub = df.sort_values("trade_date").reset_index(drop=True).tail(n)
    if "pct_chg" not in sub.columns or len(sub) < 5:
        return None
    s = sub["pct_chg"].astype(float).std()
    return float(s) if s == s else None


def fetch_analysis_bundle(
    pro,
    ts_code: str,
    *,
    trade_date: str | None,
    lookback_calendar_days: int = 550,
    fina_rows: int = 6,
) -> dict[str, Any]:
    d = trade_date or latest_open_trade_date(pro)
    start = metrics.offset_trade_calendar_days(d, delta_days=-lookback_calendar_days)

    out: dict[str, Any] = {"trade_date": d, "ts_code": ts_code, "errors": [], "stock_name": "", "stock_industry": ""}

    try:
        bi = pro.stock_basic(ts_code=ts_code, fields="ts_code,name,industry")
        if bi is not None and not bi.empty:
            out["stock_name"] = str(bi.iloc[0].get("name") or "").strip()
            out["stock_industry"] = str(bi.iloc[0].get("industry") or "").strip()
    except Exception as exc:
        out["errors"].append(f"stock_basic: {exc}")

    try:
        out["daily"] = pro.daily(ts_code=ts_code, start_date=start, end_date=d)
    except Exception as exc:
        out["daily"] = pd.DataFrame()
        out["errors"].append(f"daily: {exc}")

    daily = out.get("daily")
    if daily is not None and not daily.empty:
        daily = daily.sort_values("trade_date").reset_index(drop=True)
        out["daily"] = daily

    factor_fields = (
        "ts_code,trade_date,macd_dif_qfq,macd_dea_qfq,macd_qfq,"
        "ema_qfq_5,ema_qfq_10,ema_qfq_20,ema_qfq_60,kdj_k_qfq,kdj_d_qfq"
    )
    try:
        sf = pro.stk_factor_pro(ts_code=ts_code, trade_date=d, fields=factor_fields)
        out["stk_factor"] = sf
    except Exception as exc:
        out["stk_factor"] = None
        out["errors"].append(f"stk_factor_pro: {exc}")

    try:
        out["fina"] = pro.fina_indicator(
            ts_code=ts_code,
            fields="ts_code,end_date,ann_date,revenue_yoy,profit_dedt_yoy,roe,grossprofit_margin",
        )
    except Exception as exc:
        out["fina"] = None
        out["errors"].append(f"fina_indicator: {exc}")

    try:
        cyq = pro.cyq_perf(ts_code=ts_code, trade_date=d)
        out["cyq"] = cyq
    except Exception as exc:
        out["cyq"] = None
        out["errors"].append(f"cyq_perf: {exc}")

    try:
        db = pro.daily_basic(ts_code=ts_code, trade_date=d, fields="ts_code,trade_date,circ_mv,turnover_rate,pe_ttm,pb")
        out["daily_basic"] = db
    except Exception as exc:
        out["daily_basic"] = None
        out["errors"].append(f"daily_basic: {exc}")

    try:
        mf = pro.moneyflow_dc(ts_code=ts_code, trade_date=d)
        out["moneyflow_dc"] = mf
    except Exception as exc:
        out["moneyflow_dc"] = None
        out["errors"].append(f"moneyflow_dc: {exc}")

    if out["fina"] is not None and not out["fina"].empty:
        fi = out["fina"].sort_values(["end_date", "ann_date"], ascending=False).head(fina_rows)
        out["fina"] = fi

    return out


def build_text_report(
    bundle: dict[str, Any],
    *,
    box_window: int = 60,
    sideways_max_abs_pct_chg: float = 2.5,
    sideways_max_amplitude_pct: float = 4.0,
    sideways_scan_window: int = 120,
) -> str:
    """生成中文 Markdown：一页摘要 + 编号章节 + 表格，便于扫读。"""
    lines: list[str] = []
    ts = bundle["ts_code"]
    d = bundle["trade_date"]
    name = (bundle.get("stock_name") or "").strip()
    indu = (bundle.get("stock_industry") or "").strip()
    title_name = f"{name}（{ts}）" if name else ts
    raw_daily = bundle.get("daily")
    daily: pd.DataFrame = raw_daily if isinstance(raw_daily, pd.DataFrame) else pd.DataFrame()
    fina: pd.DataFrame | None = bundle.get("fina")

    lines.append(f"# 单票分析 · {title_name}")
    lines.append("")
    lines.append(f"> **数据交易日** `{d}`　·　Tushare Pro　·　行业：**{indu or '—'}**")
    if bundle.get("errors"):
        lines.append(f"> ⚠ 部分接口：**{'；'.join(bundle['errors'])}**")
    lines.append("")

    sw: SidewaysStats | None = None
    box: BoxStats | None = None
    s_d = s_f = s_tech = 0.0
    dbg_d: dict[str, float] = {}
    dbg_f: dict[str, float] = {}
    s_fina = 5.0
    if fina is not None and not fina.empty:
        s_fina = metrics.score_fundamental_row(fina.iloc[0])

    wr_summary: float | None = None
    cyq0 = bundle.get("cyq")
    if cyq0 is not None and not cyq0.empty:
        wr_summary = _safe_float(cyq0.iloc[0].get("winner_rate"))

    net_summary: float | None = None
    mf0 = bundle.get("moneyflow_dc")
    if mf0 is not None and not mf0.empty:
        net_summary = _safe_float(mf0.iloc[0].get("net_amount"))

    circ0 = turnover0 = None
    db0 = bundle.get("daily_basic")
    if db0 is not None and not db0.empty:
        r0 = db0.iloc[0]
        circ0 = _safe_float(r0.get("circ_mv"))
        if circ0 is None or circ0 <= 0:
            circ0 = _safe_float(r0.get("total_mv"))
        turnover0 = _safe_float(r0.get("turnover_rate"))

    last_c = last_pc = None
    if not daily.empty:
        sw = analyze_sideways(
            daily,
            max_abs_pct_chg=sideways_max_abs_pct_chg,
            max_amplitude_pct=sideways_max_amplitude_pct,
            scan_window=sideways_scan_window,
        )
        box = analyze_box(daily, window=box_window)
        s_d, dbg_d = metrics.score_technical_from_daily(daily)
        sf0 = bundle.get("stk_factor")
        if sf0 is not None and not sf0.empty:
            s_f, dbg_f = metrics.score_technical_from_factor(sf0.iloc[0])
        s_tech = metrics.merge_technical_scores(s_d, s_f, w_daily=0.5)
        last0 = daily.iloc[-1]
        last_c = _safe_float(last0.get("close"))
        last_pc = _safe_float(last0.get("pct_chg"))

    lines.append("## 一页摘要")
    lines.append("")
    if daily.empty:
        lines.append("- 暂无日线，无法展开技术面与箱体。")
    else:
        lc = f"{last_c:.2f}" if last_c is not None else "—"
        lp = f"{last_pc:+.2f}%" if last_pc is not None else "—"
        lines.append(f"- **最新收盘** **{lc}** 元，涨跌 **{lp}**")
        lines.append(f"- **技术合并分** **{s_tech:.2f}** / 10（日线 {s_d:.2f} + 因子 {s_f:.2f}）")
        lines.append(f"- **基本面粗分** **{s_fina:.2f}** / 10（按最新一期财报行）")
        if wr_summary is not None:
            cs = metrics.score_chip_winner_rate(wr_summary)
            lines.append(f"- **获利筹码** **{wr_summary:.1f}%** → 模型筹码分项 **{cs:.2f}**/10")
        else:
            lines.append("- **筹码**：未取到 `cyq_perf`（权限或数据）")
        if box is not None:
            lines.append(
                f"- **近 {box_window} 日箱体**：现价约在箱体 **{box.position_0_1 * 100:.0f}%** 高度 · {box.regime}"
            )
        if sw is not None:
            tail_txt = (
                f"`{sw.trailing_start_date}`～`{sw.trailing_end_date}`"
                if sw.trailing_days > 0 and sw.trailing_start_date and sw.trailing_end_date
                else "（最新一根不满足横盘阈值则为 0）"
            )
            long_txt = ""
            if sw.longest_streak_days > 0 and sw.longest_streak_start_date and sw.longest_streak_end_date:
                long_txt = f"；近 {sw.scan_bars} 根K内最长横盘 **`{sw.longest_streak_start_date}`～`{sw.longest_streak_end_date}`** 共 **{sw.longest_streak_days}** 天"
            elif sw.longest_streak_days == 0:
                long_txt = f"；近 {sw.scan_bars} 根K内最长横盘段为 **0** 天"
            else:
                long_txt = f"；近 {sw.scan_bars} 根K内最长横盘 **{sw.longest_streak_days}** 天"
            lines.append(f"- **横盘**：尾部连续 **{sw.trailing_days}** 日 {tail_txt}{long_txt}")
        if net_summary is not None:
            lines.append(f"- **东财大单净额**（当日）：**{net_summary:,.0f}**")
        if circ0 is not None:
            mv = f"流通市值约 **{circ0 / 10000:.1f} 亿元**"
            if turnover0 is not None:
                mv += f"，换手 **{turnover0:.1f}%**"
            lines.append(f"- {mv}")
    lines.append("")
    lines.append("---")
    lines.append("")

    if daily.empty:
        lines.append("*本报告由脚本生成，仅供研究复盘，不构成投资建议。*")
        return "\n".join(lines)

    last = daily.iloc[-1]
    o = _safe_float(last.get("open"))
    h = _safe_float(last.get("high"))
    l = _safe_float(last.get("low"))
    c = _safe_float(last.get("close"))
    pc = _safe_float(last.get("pct_chg"))
    amt = _safe_float(last.get("amount"))
    v = _safe_float(last.get("vol"))

    lines.append("## 1. 最新行情（最近一根 K 线）")
    lines.append("")
    lines.append("| 项目 | 数值 |")
    lines.append("|:--|:--|")
    ochl = " / ".join(
        [
            (f"{c:.2f}" if c is not None else "—"),
            (f"{o:.2f}" if o is not None else "—"),
            (f"{h:.2f}" if h is not None else "—"),
            (f"{l:.2f}" if l is not None else "—"),
        ]
    )
    lines.append(f"| 收 / 开 / 高 / 低 | {ochl} |")
    lines.append(f"| 涨跌幅 | **{pc:+.2f}%** |" if pc is not None else "| 涨跌幅 | — |")
    lines.append(f"| 成交额 amount | {amt:,.0f} |" if amt is not None else "| 成交额 amount | — |")
    lines.append(f"| 成交量 vol | {v:,.0f} |" if v is not None else "| 成交量 vol | — |")
    lines.append("")

    lines.append("## 2. 近期涨跌与波动")
    lines.append("")
    for n, label in ((5, "近 5 个交易日"), (20, "近 20 个交易日"), (60, "近 60 个交易日")):
        pr = _period_return(daily, n)
        if pr is not None:
            lines.append(f"- **{label}** 收盘涨跌：**{pr:+.2f}%**（相对前第 {n+1} 根 K 收盘价）")
    dd = _max_drawdown_pct(daily, min(120, len(daily)))
    if dd is not None:
        lines.append(f"- **近 {min(120, len(daily))} 根 K 最大回撤**（自前高累计）：**{dd:.2f}%**")
    vola = _volatility_pct_chg(daily, min(60, len(daily)))
    if vola is not None:
        lines.append(f"- **近 60 日波动**（涨跌幅标准差）：**{vola:.3f}%**")
    lines.append("")

    lines.append("## 3. 横盘天数")
    lines.append("")
    if sw is None:
        lines.append("- 数据过短，无法计算。")
    else:
        lines.append(
            f"**定义**：同时满足 **|涨跌|≤{sw.max_abs_pct_chg}%** 且 **振幅(高−低)/昨收×100≤{sw.max_amplitude_pct}%**（振幅分母为昨收）。"
        )
        lines.append("")
        td_tail = (
            f"`{sw.trailing_start_date}`～`{sw.trailing_end_date}`"
            if sw.trailing_days > 0 and sw.trailing_start_date and sw.trailing_end_date
            else "（最新一根不满足则为 0）"
        )
        lines.append(f"- **从最新日往前**：连续 **{sw.trailing_days}** 个交易日 {td_tail}")
        if sw.longest_streak_days > 0 and sw.longest_streak_start_date and sw.longest_streak_end_date:
            lines.append(
                f"- **近 {sw.scan_bars} 根 K 内最长一段**：**{sw.longest_streak_days}** 天（`{sw.longest_streak_start_date}`～`{sw.longest_streak_end_date}`）"
            )
        else:
            lines.append(f"- **近 {sw.scan_bars} 根 K 内最长一段**：**{sw.longest_streak_days}** 天")
        lines.append("- **读法**：连续横盘越长、箱体越窄，越像「等方向」；放量突破需量能配合。")
    lines.append("")

    lines.append("## 4. 技术面（与扫描器同源）")
    lines.append("")
    lines.append("| 项目 | 数值 |")
    lines.append("|:--|:--|")
    lines.append(f"| 合并技术分 | **{s_tech:.2f}** / 10 |")
    lines.append(f"| 日线子分 | {s_d:.2f} |")
    lines.append(f"| 因子子分 | {s_f:.2f} |")
    if dbg_d:
        vr = dbg_d.get("vol_to_ma5")
        try:
            fv = float(vr)
            vrs = f"{fv:.3f}" if fv == fv and not math.isnan(fv) else "—"
        except (TypeError, ValueError):
            vrs = "—"
        lines.append(f"| 收盘 / 前高（近 250 根已走完） | {dbg_d.get('close_to_prev_high', 0):.4f} |")
        lines.append(f"| 量比（对 5 日均量） | {vrs} |")
        lines.append(f"| 连板计数（从最新向前） | {int(dbg_d.get('limit_up_days', 0))} |")
    if dbg_f:
        ema_bull = "是" if dbg_f.get("ema_bull") else "否"
        macd_ok = "是" if dbg_f.get("macd_ok") else "否"
        kdj_p = "是" if dbg_f.get("kdj_passivated") else "否"
        lines.append(f"| 均线多头 | {ema_bull} |")
        lines.append(f"| MACD 偏多 | {macd_ok} |")
        lines.append(f"| KDJ 钝化 | {kdj_p} |")
        if "kdj_k" in dbg_f:
            lines.append(f"| KDJ 之 K | {dbg_f.get('kdj_k', 0):.2f} |")
    else:
        lines.append("| 因子侧 stk_factor_pro | 无数据（权限/积分/非交易日） |")
    lines.append("")

    lines.append("## 5. 筹码 · 市值 · 大单")
    lines.append("")
    if wr_summary is not None:
        lines.append(f"- **获利筹码**：**{wr_summary:.2f}%**（模型分项 {metrics.score_chip_winner_rate(wr_summary):.2f}/10）")
    else:
        lines.append("- **筹码**：未取到 `cyq_perf`")
    db = bundle.get("daily_basic")
    if db is not None and not db.empty:
        row = db.iloc[0]
        circ = _safe_float(row.get("circ_mv"))
        turnover = _safe_float(row.get("turnover_rate"))
        pe = _safe_float(row.get("pe_ttm"))
        pb = _safe_float(row.get("pb"))
        if circ is not None:
            lines.append(f"- **流通市值**：**{circ:,.0f}** 万元（≈ **{circ / 10000:.2f} 亿**）")
        if turnover is not None:
            lines.append(f"- **换手率**：**{turnover:.2f}%**")
        if pe is not None:
            lines.append(f"- **PE(TTM)**：{pe:.2f}")
        if pb is not None:
            lines.append(f"- **PB**：{pb:.2f}")
    mf = bundle.get("moneyflow_dc")
    if mf is not None and not mf.empty:
        net = _safe_float(mf.iloc[0].get("net_amount"))
        if net is not None:
            lines.append(f"- **东财大单净流入**：**{net:,.2f}**（单位以接口为准）")
    lines.append("")

    lines.append(f"## 6. 箱体（近 **{box_window}** 根 K 高低区间）")
    lines.append("")
    if box is None:
        lines.append("- 数据不足。")
    else:
        lines.append("| 项目 | 数值 |")
        lines.append("|:--|:--|")
        lines.append(f"| 箱顶 | **{box.top:.4f}** |")
        lines.append(f"| 箱底 | **{box.bottom:.4f}** |")
        lines.append(f"| 中轴 | {box.mid:.4f} |")
        lines.append(f"| 箱高 / 中轴 | {box.width_pct:.2f}% |")
        lines.append(f"| 现价在箱体高度 | **{box.position_0_1 * 100:.0f}%**（0=底，100=顶） |")
        lines.append(f"| 距箱顶 / 距箱底（相对现价） | **{box.dist_to_top_pct:.1f}%** / **{box.dist_to_bottom_pct:.1f}%** |")
        lines.append(f"| 形态 | {box.regime} |")
        lines.append("")
        if box.position_0_1 >= 0.85:
            lines.append("**一句话**：贴近上沿，突破看放量，防范假突破。")
        elif box.position_0_1 <= 0.15:
            lines.append("**一句话**：贴近下沿，看是否缩量企稳；破位按纪律。")
        else:
            lines.append("**一句话**：箱体中部，看量能与题材是否共振。")
    lines.append("")

    lines.append("## 7. 基本面（财报摘要）")
    lines.append("")
    if fina is None or fina.empty:
        lines.append("- 未取到 `fina_indicator`。")
    else:
        lines.append("| 报告期 | 公告日 | 营收同比% | 扣非净利同比% | ROE% | 毛利率% |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: |")
        for _, r in fina.iterrows():
            lines.append(
                f"| {r.get('end_date')} | {r.get('ann_date')} | "
                f"{_fmt_or_dash(r.get('revenue_yoy'))} | {_fmt_or_dash(r.get('profit_dedt_yoy'))} | "
                f"{_fmt_or_dash(r.get('roe'))} | {_fmt_or_dash(r.get('grossprofit_margin'))} |"
            )
        lines.append("")
        lines.append(f"最新一期粗算基本面分项：**{s_fina:.2f}**/10。季报常缺同比字段，年报/半年报更完整。")
    lines.append("")

    lines.append("## 8. 综合判断")
    lines.append("")
    lines.extend(_build_assessment(s_tech, s_fina, wr_summary, box, sw, daily))
    lines.append("")

    lines.append("## 9. 若已持仓（纪律参考）")
    lines.append("")
    lines.extend(_strategy_holding(daily, box, sw))
    lines.append("")
    lines.append("## 10. 若空仓 / 仅观察")
    lines.append("")
    lines.extend(_strategy_flat(daily, box, sw))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*本报告由脚本自动生成，仅供研究复盘，不构成投资建议。*")
    lines.append("")
    lines.append("> **完整分析建议**：结合行业新闻、券商研报、产业链动态一起阅读。")
    lines.append("> 可在 Cursor 中直接发送股票代码/简称触发本脚本，再追问行业前景即可获得联网搜索结果。")
    return "\n".join(lines)

def _build_assessment(
    s_tech: float,
    s_fina: float,
    wr: float | None,
    box: BoxStats | None,
    sw: SidewaysStats | None,
    daily: pd.DataFrame,
) -> list[str]:
    """根据已有量化数据生成「综合判断」表格 + 简要文字。"""
    lines: list[str] = []

    # --- 趋势强度判定 ---
    if s_tech >= 7:
        trend_label = "强势上攻"
    elif s_tech >= 4:
        trend_label = "偏多 / 温和上行"
    elif s_tech >= 2:
        trend_label = "中性 / 震荡"
    else:
        trend_label = "弱势 / 调整"

    # --- 筹码状态 ---
    if wr is None:
        chip_label = "未知（无数据）"
    elif wr >= 90:
        chip_label = "极端获利区（警惕获利回吐）"
    elif wr >= 70:
        chip_label = "高获利区（趋势中继或末段）"
    elif wr >= 45:
        chip_label = "中等获利（合力上攻概率较高）"
    else:
        chip_label = "低获利 / 套牢盘重"

    # --- 箱体位置 ---
    if box is None:
        box_label = "—"
    elif box.position_0_1 >= 0.85:
        box_label = "贴近上沿（突破 or 假突破）"
    elif box.position_0_1 <= 0.15:
        box_label = "贴近下沿（企稳 or 破位）"
    elif box.position_0_1 >= 0.5:
        box_label = "中上区域"
    else:
        box_label = "中下区域"

    # --- 基本面 ---
    if s_fina >= 7:
        fina_label = "良好（成长 + 盈利双高）"
    elif s_fina >= 5:
        fina_label = "一般 / 中性"
    else:
        fina_label = "偏弱 / 亏损或缺数据"

    # --- 近期涨幅 ---
    ret20 = _period_return(daily, 20)
    if ret20 is not None and ret20 > 30:
        momentum_label = f"短期急拉 +{ret20:.0f}%（追高风险大）"
    elif ret20 is not None and ret20 > 15:
        momentum_label = f"中强 +{ret20:.0f}%（右侧趋势）"
    elif ret20 is not None and ret20 > 0:
        momentum_label = f"温和上涨 +{ret20:.0f}%"
    elif ret20 is not None:
        momentum_label = f"回调 {ret20:+.0f}%"
    else:
        momentum_label = "—"

    # --- 横盘收敛 ---
    if sw is not None and sw.trailing_days >= 5:
        sideways_label = f"尾部横盘 {sw.trailing_days} 天（等方向选择）"
    elif sw is not None and sw.trailing_days > 0:
        sideways_label = f"轻微收敛 {sw.trailing_days} 天"
    else:
        sideways_label = "无横盘（趋势段内）"

    lines.append("| 维度 | 评价 |")
    lines.append("|:--|:--|")
    lines.append(f"| **趋势强度** | {trend_label}（技术合并分 {s_tech:.1f}/10） |")
    lines.append(f"| **基本面** | {fina_label}（粗分 {s_fina:.1f}/10） |")
    lines.append(f"| **筹码状态** | {chip_label} |")
    lines.append(f"| **箱体位置** | {box_label} |")
    lines.append(f"| **近 20 日动量** | {momentum_label} |")
    lines.append(f"| **横盘收敛** | {sideways_label} |")
    lines.append("")

    # 一句话总结
    risk_flags = 0
    if wr is not None and wr >= 90:
        risk_flags += 1
    if ret20 is not None and ret20 > 30:
        risk_flags += 1
    if box is not None and box.position_0_1 >= 0.85:
        risk_flags += 1

    if risk_flags >= 2:
        lines.append("**一句话**：多项短期过热信号共振，追高性价比低；右侧等回踩确认更稳妥。")
    elif s_tech >= 6 and (sw is not None and sw.trailing_days >= 5):
        lines.append("**一句话**：技术偏强 + 横盘收敛，关注放量突破信号。")
    elif s_tech <= 2:
        lines.append("**一句话**：技术面偏弱，以观望/轻仓试错为主，等待企稳信号。")
    else:
        lines.append("**一句话**：当前处于中间状态，结合板块热度与资金流向进一步判断方向。")

    return lines


def _fmt_or_dash(x: Any) -> str:
    v = _safe_float(x)
    if v is None:
        return "—"
    return f"{v:.2f}"


def _strategy_holding(daily: pd.DataFrame, box: BoxStats | None, sw: SidewaysStats | None) -> list[str]:
    out: list[str] = []
    out.append("- **纪律**：先有止损/减仓规则，再谈加仓；单票仓位与总风险上限自行约束。")
    if box is not None:
        out.append(
            f"- **箱底防守**：有效跌破箱底 **{box.bottom:.4f}**（例如连续两日收盘低于箱底约 **1%~2%**）"
            f" 时，按计划减仓或止损；箱底仅作参考，非「铁底」。"
        )
        out.append(
            f"- **箱顶附近**：现价距箱顶约 **{box.dist_to_top_pct:.2f}%**；若已在高位且放量滞涨，可分批止盈，保留底仓观察突破。"
        )
        out.append(
            f"- **中轴回踩**：若趋势仍强，部分资金会以箱中轴 **{box.mid:.4f}** 附近作为加减仓的参考带，需结合均线与量能。"
        )
    else:
        out.append("- 箱体数据不足时：用最近一波显著低点下方 **3%~5%** 作为技术止损参考（自行调整）。")

    if not daily.empty:
        s_d, dbg = metrics.score_technical_from_daily(daily)
        lim = int(dbg.get("limit_up_days") or 0)
        if lim >= 2:
            out.append("- **连板后持仓**：波动极大；可用「前一日低点」或「5 日均线」作为移动止盈参考，避免利润大幅回吐。")
        ratio = dbg.get("close_to_prev_high")
        if ratio is not None and float(ratio) >= 1.02:
            out.append("- **前高附近**：处于突破或假突破敏感区；假突破可参考「跌回突破位下约 3% 且短期收不回」处理。")
    if sw is not None and sw.trailing_days >= 5:
        out.append(
            f"- **横盘后持仓**：已连续横盘 **{sw.trailing_days}** 日，若持仓成本低可保留观察；"
            "一旦放量跌破横盘区下沿或长期均线，按纪律减仓。"
        )
    return out


def _strategy_flat(daily: pd.DataFrame, box: BoxStats | None, sw: SidewaysStats | None) -> list[str]:
    out: list[str] = []
    out.append("- **不追直线**：急拉、一字、缩量加速段默认只观察；右侧买点优先等「回踩 + 缩量 + 再放量」。")
    if box is not None:
        out.append(
            f"- **箱体上沿突破**：若放量站稳箱顶 **{box.top:.4f}** 之上，可等待回踩确认再上；避免首日情绪最高点满仓。"
        )
        out.append(
            f"- **箱体下沿博弈**：接近箱底 **{box.bottom:.4f}** 时，更适合小仓试错；破位则放弃，不摊平。"
        )
        if box.width_pct < 10:
            out.append("- **窄幅横盘**：突破方向不明前，空仓者可减少「箱内频繁刷单」，等待方向选择。")
        if sw is not None and sw.trailing_days >= 5 and box.width_pct < 15:
            out.append(
                f"- **横盘尾声**：近端连续横盘 **{sw.trailing_days}** 日且箱体不高，空仓者可等放量突破箱顶并回踩确认后再考虑右侧。"
            )
    else:
        out.append("- 无明确箱体时：用「均线多头排列 + 回调缩量」或「大盘/板块共振」作为入场过滤器。")

    if not daily.empty:
        s_d, dbg = metrics.score_technical_from_daily(daily)
        vr = dbg.get("vol_to_ma5")
        if vr is not None and float(vr) >= 2.0:
            out.append("- **放量剧烈震荡日**：空仓者默认观望一日，避免在情绪极值点接盘。")
    return out
