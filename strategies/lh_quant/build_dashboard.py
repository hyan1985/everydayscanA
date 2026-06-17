#!/usr/bin/env python3
"""
生成本地展示页: output/dashboard.html
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    PLOTLY_AVAILABLE = True
except Exception:
    PLOTLY_AVAILABLE = False


def latest_daily_file(output_dir: Path) -> Path | None:
    daily_dir = output_dir / "daily"
    files = [
        p
        for p in daily_dir.glob("daily_selection_*_cn.csv")
        if "_universe" not in p.name
    ]
    if not files:
        return None
    # 按文件修改时间取最新（兼容 daily_selection_YYYYMMDD 与 daily_selection_YYYY-MM-DD 等命名）
    return max(files, key=lambda p: p.stat().st_mtime)


def latest_box_signal_file(output_dir: Path) -> Path | None:
    box_dir = output_dir / "box_range_monitor"
    files = sorted(box_dir.glob("box_signals_*.csv"))
    return files[-1] if files else None


def load_box_name_map(output_dir: Path) -> dict[str, str]:
    cfg_path = output_dir.parent / "config" / "box_monitor_config.json"
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw = data.get("symbol_name_map", {})
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}


def df_to_html(df: pd.DataFrame) -> str:
    if df.empty:
        return "<p>暂无数据</p>"
    return df.to_html(index=False, border=0, classes="table", justify="left", escape=False)


def pick_dashboard_candidates(
    daily_df: pd.DataFrame,
    top_k: int,
) -> tuple[pd.DataFrame, str]:
    """
    若有「是否买入信号」列则展示全部买入信号行；否则按排序取前 top_k（宽表 / 旧文件）。
    返回 (子集, 用于标题的简短说明)。
    """
    if daily_df.empty:
        return daily_df, ""
    for col in ("是否买入信号", "is_trade_signal"):
        if col not in daily_df.columns:
            continue
        ser = daily_df[col]
        if ser.dtype == bool:
            mask = ser
        else:
            mask = (
                ser.astype(str)
                .str.strip()
                .str.lower()
                .isin(("true", "1", "yes"))
            )
        sig_df = daily_df.loc[mask].copy()
        if not sig_df.empty:
            return sig_df, f"交易信号共 {len(sig_df)} 只"
    k = min(top_k, len(daily_df)) if top_k > 0 else len(daily_df)
    out = daily_df.head(k).copy()
    return out, f"排序 Top {len(out)}（未区分信号列）"


def pick_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return df
    keep = [c for c in cols if c in df.columns]
    return df[keep].copy()


def enrich_box_names(
    box_df: pd.DataFrame,
    daily_df: pd.DataFrame,
    journal_df: pd.DataFrame,
    config_name_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    if box_df.empty:
        return box_df
    out = box_df.copy()
    code_col = "symbol" if "symbol" in out.columns else None
    if not code_col:
        return out

    name_map: dict[str, str] = {}
    if config_name_map:
        name_map.update(config_name_map)
    if not daily_df.empty and "股票代码" in daily_df.columns and "股票名称" in daily_df.columns:
        tmp = daily_df[["股票代码", "股票名称"]].dropna().drop_duplicates(subset=["股票代码"])
        name_map.update({str(r["股票代码"]): str(r["股票名称"]) for _, r in tmp.iterrows()})
    if not journal_df.empty and "ts_code" in journal_df.columns and "name" in journal_df.columns:
        tmp = journal_df[["ts_code", "name"]].dropna().drop_duplicates(subset=["ts_code"])
        for _, r in tmp.iterrows():
            code = str(r["ts_code"])
            if code not in name_map:
                name_map[code] = str(r["name"])

    out["name"] = out[code_col].map(name_map).fillna("-")
    return out


def localize_box_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df_new = df.copy()
    if "signal" in df_new.columns:
        box_sig_map = {
            "BREAKOUT_UP": "放量突破",
            "FAKE_BREAKOUT": "假突破预警",
            "PULLBACK_SUPPORT": "缩量回踩确认",
            "BREAKDOWN_DOWN": "向下跌破",
            "BUY_ZONE": "近下沿(买入区)",
            "SELL_ZONE": "近上沿(卖出区)",
            "HOLD": "箱体中部(观望)"
        }
        df_new["signal"] = df_new["signal"].map(lambda x: box_sig_map.get(str(x).strip(), str(x)))
    
    rename_map = {
        "symbol": "股票代码",
        "name": "股票名称",
        "date": "日期",
        "close": "最新价",
        "box_lower": "箱体下沿",
        "box_upper": "箱体上沿",
        "dist_to_lower": "距下沿比例",
        "dist_to_upper": "距上沿比例",
        "signal": "信号",
        "hint": "提示",
    }
    return df_new.rename(columns=rename_map)


def describe_entry_type(x: str) -> str:
    m = {
        "cross_flow": "金叉资金共振（优先）",
        "breakout": "突破确认",
        "setup": "埋伏启动",
        "none": "无",
    }
    s = str(x).strip()
    return m.get(s, s)


def decision_rating_for_row(row: pd.Series) -> str:
    """
    与 searchv1 一致：仅有 entry_signal_ok=True 时为买入级；其余 Top 榜为观察池排序。
    """
    tier = row.get("selection_tier", row.get("名单类型"))
    its = row.get("is_trade_signal", row.get("是否买入信号"))
    if pd.notna(tier):
        s = str(tier).strip().lower()
        if s == "signal":
            return "买入信号"
        if s == "watchlist":
            return "观察（非买入）"
    if its is True or its == 1:
        return "买入信号"
    if its is False or its == 0:
        return "观察（非买入）"
    if str(its).strip().lower() in ("true", "false"):
        return "买入信号" if str(its).strip().lower() == "true" else "观察（非买入）"
    return "未知"


def attach_decision_rating(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["决策评级"] = out.apply(decision_rating_for_row, axis=1)
    return out


def soften_non_buy_plans(df: pd.DataFrame) -> pd.DataFrame:
    """
    观察池/未知评级：不展示「可执行」建仓止盈止损话术，避免误读为买入指令。
    """
    if df.empty or "决策评级" not in df.columns:
        return df
    out = df.copy()
    plan_cols = ["建仓策略", "持仓策略", "止盈策略", "止损策略"]
    for c in plan_cols:
        if c not in out.columns:
            return out

    watch_build = (
        "【观察】当前未满足算法买入条件，默认不建仓。"
        "若仅跟踪盘面，请自行设定触发条件与仓位；表中触发价与止盈止损数字仅为原始输出占位，不作为操作建议。"
    )
    watch_hold = "【观察】无预设持仓方案。"
    watch_tp = "【观察】不设止盈执行档位；表中价位为占位，请忽略。"
    watch_sl = "【观察】不设止损执行价位；表中价位为占位，请忽略。"
    unk_build = (
        "【待分级】缺少名单类型字段，请勿按表中价位执行；确认是否为买入信号后再制定计划。"
    )
    price_cols = ("触发价", "硬止损价", "止盈一档价", "止盈二档价")
    dash = "—（观察不适用）"
    for pc in price_cols:
        if pc in out.columns:
            out[pc] = out[pc].astype(object)

    for i, row in out.iterrows():
        rating = str(row.get("决策评级", "")).strip()
        if rating == "买入信号":
            continue
        if rating == "未知":
            out.at[i, "建仓策略"] = unk_build
            out.at[i, "持仓策略"] = watch_hold
            out.at[i, "止盈策略"] = watch_tp
            out.at[i, "止损策略"] = watch_sl
        else:
            out.at[i, "建仓策略"] = watch_build
            out.at[i, "持仓策略"] = watch_hold
            out.at[i, "止盈策略"] = watch_tp
            out.at[i, "止损策略"] = watch_sl
        for pc in price_cols:
            if pc in out.columns:
                out.at[i, pc] = dash
    return out


def describe_score_level(v: float, high: float, mid: float, labels: tuple[str, str, str]) -> str:
    try:
        x = float(v)
    except Exception:
        return "暂无"
    if x >= high:
        lv = labels[0]
    elif x >= mid:
        lv = labels[1]
    else:
        lv = labels[2]
    return f"{lv}（{x:.1f}）"


def normalize_strategy_texts(df: pd.DataFrame) -> pd.DataFrame:
    """
    将策略卡文案改写为更自然的执行语言，并补齐关键价格。
    """
    if df.empty:
        return df
    out = df.copy()

    def _fmt_price(v) -> str:
        try:
            return f"{float(v):.2f}"
        except Exception:
            return "N/A"

    for i, row in out.iterrows():
        entry_type = str(row.get("入场类型", "")).strip()
        trigger = _fmt_price(row.get("触发价"))
        tp1 = _fmt_price(row.get("止盈一档价"))
        tp2 = _fmt_price(row.get("止盈二档价"))
        hard = _fmt_price(row.get("硬止损价"))

        if "金叉资金共振" in entry_type:
            build_short = f"50%先手，站稳{trigger}后补满"
            build_plan = f"先建仓50%，当价格站稳触发价 {trigger} 后再补仓50%。"
        elif "突破" in entry_type:
            build_short = f"突破{trigger}后分两笔建仓"
            build_plan = f"当价格突破触发价 {trigger} 时分两笔建仓（50%+50%）。"
        elif "埋伏" in entry_type or "setup" in entry_type.lower():
            build_short = f"40%试仓，确认后补仓到满"
            build_plan = f"先试仓40%，若回踩不破并重新走强再加仓60%（参考触发价 {trigger}）。"
        else:
            build_short = f"观望，关注触发价{trigger}"
            build_plan = f"当前无明确入场信号，先观察；参考触发价 {trigger}。"

        hold_short = "先看止盈，再看趋势衰减，最长30日"
        hold_plan = "持仓期间每天跟踪：先看止盈一档/二档是否触发，再看趋势是否衰减（MA5与MACD）。最长持有30个交易日。"
        tp_short = f"{tp1}/{tp2} 两档止盈"
        tp_plan = f"止盈分两档执行：到 {tp1} 先减仓一部分，到 {tp2} 再继续止盈。"
        sl_short = f"跌破{hard}止损"
        sl_plan = f"硬止损价位 {hard}，若价格跌破应执行止损；若趋势明显转弱可提前离场。"

        out.at[i, "建仓策略"] = build_plan
        out.at[i, "持仓策略"] = hold_plan
        out.at[i, "止盈策略"] = tp_plan
        out.at[i, "止损策略"] = sl_plan

    return out


def build_score_compare_chart(top_df: pd.DataFrame) -> str:
    """
    Top-K 候选股的多维分数对比柱状图:
    - 突破前夜分 / 吸筹分 / 板块轮动分 / 总分
    """
    if not PLOTLY_AVAILABLE or top_df.empty:
        return ""
    candidates = []
    for col in ("突破前夜分", "吸筹分", "板块轮动分", "总分"):
        if col in top_df.columns:
            candidates.append(col)
    if not candidates:
        return ""

    code_col = "股票代码" if "股票代码" in top_df.columns else None
    name_col = "股票名称" if "股票名称" in top_df.columns else None
    if not code_col:
        return ""
    labels = []
    for _, row in top_df.iterrows():
        code = str(row.get(code_col, ""))
        nm = str(row.get(name_col, "")) if name_col else ""
        labels.append(f"{nm}\n{code}" if nm else code)

    fig = go.Figure()
    palette = ["#3B82F6", "#10B981", "#F59E0B", "#EF4444"]
    for i, col in enumerate(candidates):
        vals = pd.to_numeric(top_df[col], errors="coerce").fillna(0)
        fig.add_trace(
            go.Bar(
                name=col,
                x=labels,
                y=vals,
                marker_color=palette[i % len(palette)],
                text=[f"{float(v):.1f}" for v in vals],
                textposition="outside",
            )
        )
    fig.update_layout(
        barmode="group",
        title="Top 候选 - 多维分数对比",
        xaxis_title="",
        yaxis_title="分数(0-100)",
        yaxis=dict(range=[0, 110]),
        height=380,
        margin=dict(l=40, r=20, t=50, b=80),
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(orientation="h", y=-0.18),
    )
    fig.update_xaxes(showgrid=False, tickangle=0)
    fig.update_yaxes(gridcolor="#e5e7eb")
    return fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displaylogo": False})


def find_recent_kline_csv(cache_dir: Path, ts_code: str) -> Path | None:
    """在 backtest_cache/daily/ 中找该 ts_code 最近的缓存文件(按文件名结尾日期排序)。"""
    if not cache_dir.exists():
        return None
    files = sorted(cache_dir.glob(f"{ts_code}_*.csv"))
    return files[-1] if files else None


def build_kline_chart(top_df: pd.DataFrame, project_root: Path, max_charts: int = 3) -> str:
    """对 Top-K 候选股,各画一张 K线 + MA5/10/20 + 量能图。数据源: backtest_cache/daily/。"""
    if not PLOTLY_AVAILABLE or top_df.empty:
        return ""
    cache_dir = project_root / "backtest_cache" / "daily"
    code_col = "股票代码" if "股票代码" in top_df.columns else None
    name_col = "股票名称" if "股票名称" in top_df.columns else None
    if not code_col:
        return ""

    blocks: list[str] = []
    for _, row in top_df.head(max_charts).iterrows():
        code = str(row.get(code_col, "")).strip()
        if not code:
            continue
        f = find_recent_kline_csv(cache_dir, code)
        if not f:
            blocks.append(
                f"<div class='hint'>· {code} 无本地缓存 K 线（先跑一次 run_backtest 或忽略）</div>"
            )
            continue
        try:
            df = pd.read_csv(f, parse_dates=["trade_date"])
        except Exception:
            continue
        if df.empty:
            continue
        df = df.sort_values("trade_date").tail(120)
        nm = str(row.get(name_col, "")) if name_col else ""
        title = f"{nm} {code} ({df['trade_date'].iloc[-1].strftime('%Y-%m-%d')} 收盘 {df['close'].iloc[-1]:.2f})"

        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.04,
            subplot_titles=("价格 & 均线", "成交量"),
        )
        fig.add_trace(
            go.Candlestick(
                x=df["trade_date"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                name="K线", increasing_line_color="#ef4444", decreasing_line_color="#10b981",
            ),
            row=1, col=1,
        )
        for n, color in ((5, "#3b82f6"), (10, "#f59e0b"), (20, "#a855f7")):
            ma = df["close"].rolling(n).mean()
            fig.add_trace(
                go.Scatter(x=df["trade_date"], y=ma, name=f"MA{n}", line=dict(color=color, width=1.4)),
                row=1, col=1,
            )
        vol_color = ["#ef4444" if c >= o else "#10b981" for c, o in zip(df["close"], df["open"])]
        fig.add_trace(
            go.Bar(x=df["trade_date"], y=df["volume"], marker_color=vol_color, name="成交量", showlegend=False),
            row=2, col=1,
        )
        fig.update_layout(
            title=title, height=420, margin=dict(l=40, r=20, t=60, b=30),
            plot_bgcolor="white", paper_bgcolor="white",
            legend=dict(orientation="h", y=-0.12),
            xaxis_rangeslider_visible=False,
        )
        fig.update_xaxes(gridcolor="#f3f4f6")
        fig.update_yaxes(gridcolor="#f3f4f6")
        blocks.append(fig.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False}))

    return "\n".join(blocks)


def main():
    p = argparse.ArgumentParser(description="生成信号展示页")
    p.add_argument("--output-dir", default="output")
    p.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="无「是否买入信号」列时，取排序前 K 行；有信号列时忽略此上限（展示全部信号）",
    )
    p.add_argument(
        "--max-kline",
        type=int,
        default=15,
        help="K 线图最多绘制几只（避免信号过多时页面过重）",
    )
    args = p.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    project_root = out_dir.parent if out_dir.name == "output" else Path.cwd()
    daily_file = latest_daily_file(out_dir)
    daily_df = pd.read_csv(daily_file) if daily_file and daily_file.exists() else pd.DataFrame()
    top_df, top_kind = pick_dashboard_candidates(daily_df, args.top_k)
    score_chart_html = build_score_compare_chart(top_df) if not top_df.empty else ""
    kline_cap = max(1, min(args.max_kline, len(top_df))) if not top_df.empty else 0
    kline_badge_n = str(kline_cap) if kline_cap else "—"
    kline_limit_hint = (
        f"信号较多时仅绘制前 {kline_cap} 只以免页面过大。" if kline_cap else ""
    )
    kline_chart_html = (
        build_kline_chart(top_df, project_root, max_charts=kline_cap) if not top_df.empty else ""
    )
    if not top_df.empty:
        if "入场类型" in top_df.columns:
            top_df["入场类型"] = top_df["入场类型"].map(describe_entry_type)
        if "W&R趋势分" in top_df.columns:
            top_df["W&R趋势分"] = top_df["W&R趋势分"].map(
                lambda x: describe_score_level(x, high=70, mid=55, labels=("强势", "中性", "偏弱"))
            )
        if "板块轮动分" in top_df.columns:
            top_df["板块轮动分"] = top_df["板块轮动分"].map(
                lambda x: describe_score_level(x, high=60, mid=45, labels=("热门", "中性", "偏弱"))
            )
        if "箱体信号" in top_df.columns:
            box_sig_map = {
                "BREAKOUT_UP": "放量突破",
                "FAKE_BREAKOUT": "假突破预警",
                "PULLBACK_SUPPORT": "缩量回踩确认",
                "BREAKDOWN_DOWN": "向下跌破",
                "BUY_ZONE": "近下沿(买入区)",
                "SELL_ZONE": "近上沿(卖出区)",
                "HOLD": "箱体中部(观望)"
            }
            top_df["箱体信号"] = top_df["箱体信号"].map(lambda x: box_sig_map.get(str(x).strip(), str(x)))
        top_df = normalize_strategy_texts(top_df)
        top_df = attach_decision_rating(top_df)
        top_df = soften_non_buy_plans(top_df)
    buy_n = int((top_df["决策评级"] == "买入信号").sum()) if not top_df.empty else 0
    watch_n = int((top_df["决策评级"] == "观察（非买入）").sum()) if not top_df.empty else 0
    unk_n = int((top_df["决策评级"] == "未知").sum()) if not top_df.empty else 0
    if buy_n == 0 and not top_df.empty:
        banner_cls = "banner banner-warn"
        banner_extra = (
            "<strong>注意：</strong>当前 Top 榜单<strong>未包含算法确认的买入信号</strong>（均为观察池排序）。"
            "下方建仓/止盈止损仅供<strong>假设已入场</strong>的格式参考，请勿默认按满仓执行。"
        )
    elif buy_n > 0:
        banner_cls = "banner banner-ok"
        banner_extra = "请优先处理标记为「买入信号」的标的；其余为跟踪观察。"
    else:
        banner_cls = "banner banner-muted"
        banner_extra = ""
    unk_part = f"，名单层级未识别 {unk_n} 只" if unk_n else ""
    banner_html = (
        f'<div class="{banner_cls}">'
        f"<strong>决策评级汇总：</strong>本页共 <strong>{len(top_df)}</strong> 条（{top_kind}），其中 "
        f"<strong>买入信号 {buy_n}</strong> 只，"
        f"<strong>观察（非买入）{watch_n}</strong> 只{unk_part}。"
        f" {banner_extra}</div>"
    )
    top_core_df = pick_cols(
        top_df,
        [
            "股票代码",
            "股票名称",
            "决策评级",
            "箱体信号",
            "入场类型",
            "总分",
            "最新价",
            "板块轮动分",
            "风险提示",
        ],
    )
    top_plan_df = pick_cols(
        top_df,
        [
            "股票代码",
            "股票名称",
            "决策评级",
            "建仓策略",
            "持仓策略",
            "止盈策略",
            "止损策略",
        ],
    )

    box_file = latest_box_signal_file(out_dir)
    box_df = pd.read_csv(box_file) if box_file and box_file.exists() else pd.DataFrame()
    config_name_map = load_box_name_map(out_dir)
    if not box_df.empty and "signal" in box_df.columns:
        actionable = {"BUY_ZONE", "SELL_ZONE", "BREAKOUT_UP", "BREAKDOWN_DOWN"}
        box_trigger_df = box_df[box_df["signal"].isin(actionable)].copy()
    else:
        box_trigger_df = pd.DataFrame()

    journal_file = out_dir / "journal" / "signal_journal.csv"
    journal = pd.read_csv(journal_file) if journal_file.exists() else pd.DataFrame()

    box_df = enrich_box_names(
        box_df=box_df,
        daily_df=daily_df,
        journal_df=journal,
        config_name_map=config_name_map,
    )
    box_trigger_df = enrich_box_names(
        box_trigger_df,
        daily_df=daily_df,
        journal_df=journal,
        config_name_map=config_name_map,
    )
    box_table = pick_cols(
        localize_box_columns(box_df),
        ["股票代码", "股票名称", "日期", "最新价", "箱体下沿", "箱体上沿", "距下沿比例", "距上沿比例", "信号", "提示"],
    )
    box_trigger_table = pick_cols(
        localize_box_columns(box_trigger_df),
        ["股票代码", "股票名称", "日期", "最新价", "箱体下沿", "箱体上沿", "距下沿比例", "距上沿比例", "信号", "提示"],
    )

    if not journal.empty:
        w5 = journal["is_win_5d"].dropna()
        w10 = journal["is_win_10d"].dropna()
        win5 = f"{float(w5.mean()):.2%}" if len(w5) else "N/A"
        win10 = f"{float(w10.mean()):.2%}" if len(w10) else "N/A"
        sample5 = len(w5)
        sample10 = len(w10)
    else:
        win5, win10, sample5, sample10 = "N/A", "N/A", 0, 0

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>选股执行看板</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 0; color: #111827; background: #f3f4f6; }}
    .wrap {{ max-width: 1400px; margin: 0 auto; padding: 20px 22px 36px; }}
    h1 {{ margin: 0 0 10px 0; font-size: 28px; }}
    h2 {{ margin: 0 0 10px 0; font-size: 20px; }}
    .meta {{ color: #6b7280; margin-bottom: 16px; font-size: 14px; }}
    .section {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 14px 16px; margin-bottom: 14px; box-shadow: 0 1px 1px rgba(0,0,0,0.02); }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 10px; margin-bottom: 4px; }}
    .card {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px; background: #fafafa; }}
    .k {{ font-size: 12px; color: #6b7280; }}
    .v {{ font-size: 22px; font-weight: 700; margin-top: 3px; }}
    .table-wrap {{ overflow-x: auto; }}
    .table {{ width: 100%; border-collapse: collapse; font-size: 13px; table-layout: auto; }}
    .table th, .table td {{ border-bottom: 1px solid #e5e7eb; padding: 8px 8px; text-align: left; vertical-align: top; }}
    .table th {{ background: #f9fafb; position: sticky; top: 0; z-index: 1; white-space: nowrap; }}
    .table tr:nth-child(even) td {{ background: #fcfcfd; }}
    .table td {{ word-break: break-word; line-height: 1.35; }}
    .hint {{ margin: 6px 0 10px; color: #4b5563; font-size: 13px; }}
    ul {{ margin: 8px 0 0 18px; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; background:#eef2ff; color:#3730a3; }}
    .compact-title {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom: 8px; }}
    .banner {{ padding: 12px 14px; border-radius: 10px; margin-bottom: 14px; font-size: 14px; line-height: 1.55; }}
    .banner-warn {{ background: #fef3c7; border: 1px solid #f59e0b; color: #92400e; }}
    .banner-ok {{ background: #ecfdf5; border: 1px solid #10b981; color: #065f46; }}
    .banner-muted {{ background: #f3f4f6; border: 1px solid #e5e7eb; color: #4b5563; }}
  </style>
</head>
<body>
  <div class="wrap">
  <h1>选股执行看板</h1>
  <div class="meta">更新时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} | 数据源: {daily_file.name if daily_file else "N/A"}</div>
  {banner_html}

  <div id="sec-metrics" class="section">
  <div class="grid">
    <div class="card"><div class="k">5日胜率（已结算样本）</div><div class="v">{win5}</div><div class="k">样本: {sample5}</div></div>
    <div class="card"><div class="k">10日胜率（已结算样本）</div><div class="v">{win10}</div><div class="k">样本: {sample10}</div></div>
    <div class="card"><div class="k">今日候选</div><div class="v">{len(top_core_df)}</div><div class="k">{top_kind}</div></div>
    <div class="card"><div class="k">箱体触发数（固定观察池）</div><div class="v">{len(box_trigger_table)}</div><div class="k">观察池: {len(box_table)}</div></div>
  </div>
  </div>

  <div id="sec-core" class="section">
  <div class="compact-title"><h2>今日核心决策信息 · {top_kind}</h2></div>
  <div class="hint">「买入信号」表示当日算法入场条件已全部满足；「观察」表示仅排序靠前，<strong>不等于买入指令</strong>。详见上方提示条。</div>
  <div class="table-wrap">{df_to_html(top_core_df)}</div>
  </div>

  <div id="sec-scores" class="section">
  <div class="compact-title"><h2>多维分数对比</h2><span class="badge">突破前夜分 / 吸筹分 / 板块轮动分 / 总分</span></div>
  <div class="hint">用 4 根柱子横向对比，找"突破前夜分高 + 吸筹分高 + 涨幅未透支"的票。</div>
  {score_chart_html if score_chart_html else "<p>无可用分数数据</p>"}
  </div>

  <div id="sec-kline" class="section">
  <div class="compact-title"><h2>关键时序（最近 ~120 交易日）</h2><span class="badge">K 线 + MA5/10/20 + 量能 · 最多{kline_badge_n}只</span></div>
  <div class="hint">K 线数据来自 backtest_cache/daily/，若候选股无缓存请先跑一次 ./scripts/run_backtest.sh。{kline_limit_hint}</div>
  {kline_chart_html if kline_chart_html else "<p>暂无 K 线缓存</p>"}
  </div>

  <div id="sec-plan" class="section">
  <div class="compact-title"><h2>每票执行策略卡</h2><span class="badge">建仓/持仓/止盈/止损</span></div>
  <div class="hint">仅「买入信号」行为可执行范式；「观察」行为占位说明，价位列已置为「—」。</div>
  <div class="table-wrap">{df_to_html(top_plan_df)}</div>
  </div>

  <div id="sec-box" class="section">
  <h2>箱体波段监控（固定观察池）</h2>
  <div class="hint">由 ./scripts/run_daily.sh 同步触发，当前数据文件: {box_file.name if box_file else "N/A"}</div>
  <h2>箱体触发清单</h2>
  <div class="table-wrap">{df_to_html(box_trigger_table)}</div>
  <h2>箱体观察池全量状态</h2>
  <div class="table-wrap">{df_to_html(box_table)}</div>
  </div>
  </div>
</body>
</html>
"""

    dash_dir = out_dir / "dashboard"
    dash_dir.mkdir(parents=True, exist_ok=True)
    target = dash_dir / "dashboard.html"
    target.write_text(html, encoding="utf-8")
    print(f"看板已生成: {target}")


if __name__ == "__main__":
    main()

