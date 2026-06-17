from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .models import MarketSnapshot, SectorSnapshot, StockSnapshot
from .scoring import evaluate_emotion, pick_top_sectors, score_stock


def _load_input(path: Path) -> tuple[dict, MarketSnapshot, list[SectorSnapshot], list[StockSnapshot]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    market = MarketSnapshot(**data["market"])
    sectors = [SectorSnapshot(**i) for i in data["sectors"]]
    stocks = [StockSnapshot(**i) for i in data["stocks"]]
    meta = data.get("meta", {})
    return meta, market, sectors, stocks


def _load_performance_history() -> pd.DataFrame:
    history = Path("data/performance_history.csv")
    if not history.exists():
        return pd.DataFrame()
    df = pd.read_csv(history)
    if df.empty:
        return pd.DataFrame()
    required = {"pick_date", "eval_date", "code", "name", "total_score", "pick_close", "eval_close", "close_vs_pick_close_pct", "is_win"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()
    df["pick_date"] = df["pick_date"].astype(str)
    df["eval_date"] = df["eval_date"].astype(str)
    df["code"] = df["code"].astype(str)
    return df


def _load_performance() -> pd.DataFrame:
    df = _load_performance_history()
    if df.empty:
        return df
    latest_eval = df["eval_date"].max()
    out = df[df["eval_date"] == latest_eval].copy()
    if out.empty:
        return out
    out = out.sort_values(by="total_score", ascending=False)
    return out


def _build_sector_formation(score_df: pd.DataFrame, top_sectors: list[str]) -> pd.DataFrame:
    rows: list[dict] = []
    for sector in top_sectors:
        sec = score_df[score_df["板块"] == sector].sort_values(by=["总分", "追随"], ascending=False)
        if sec.empty:
            continue
        leader = sec.iloc[0]
        follower_1 = sec.iloc[1] if len(sec) > 1 else None
        follower_2 = sec.iloc[2] if len(sec) > 2 else None
        leader_action = str(leader.get("建议", ""))
        follower_1_action = str(follower_1.get("建议", "")) if follower_1 is not None else ""
        follower_2_action = str(follower_2.get("建议", "")) if follower_2 is not None else ""
        rows.append(
            {
                "板块": sector,
                "龙头": f"{leader['名称']}({leader['代码']})",
                "龙头建议": leader_action,
                "追随1": (
                    f"{follower_1['名称']}({follower_1['代码']}) / 追随{follower_1['追随']}"
                    if follower_1 is not None
                    else "样本不足"
                ),
                "追随1建议": follower_1_action,
                "追随2": (
                    f"{follower_2['名称']}({follower_2['代码']}) / 追随{follower_2['追随']}"
                    if follower_2 is not None
                    else "样本不足"
                ),
                "追随2建议": follower_2_action,
                "板块平均追随": round(float(sec["追随"].mean()), 2),
                "板块涨幅": round(float(sec["板块涨幅"].mean()), 2),
            }
        )
    return pd.DataFrame(rows)


def render_static_dashboard(input_path: Path, output_path: Path, minimal_n: int = 5) -> Path:
    meta, market, sectors, stocks = _load_input(input_path)
    emotion, position_advice, principle = evaluate_emotion(market)
    top_sectors = pick_top_sectors(sectors, n=3)
    strongest = {s.name for s in top_sectors}
    scores = sorted(
        [score_stock(stock, strongest_sectors=strongest) for stock in stocks],
        key=lambda x: x.total_score,
        reverse=True,
    )

    score_df = pd.DataFrame(
        [
            {
                "名称": s.name,
                "代码": s.code,
                "总分": s.total_score,
                "量能": s.score_liquidity,
                "位置": s.score_price_position,
                "题材": s.score_theme_status,
                "人气": s.score_popularity,
                "追随": s.follow_score,
                "建议": s.action,
                "标签": " / ".join(s.tags) if s.tags else "-",
                "板块": s.sector,
                "板块涨幅": next((x.pct_chg for x in sectors if x.name == s.sector), 0.0),
            }
            for s in scores
        ]
    )
    top_sector_names = [s.name for s in sectors[: min(6, len(sectors))]]
    formation_df = _build_sector_formation(score_df, top_sector_names)
    def _badge(action: str) -> str:
        action = str(action or "")
        if action == "可做":
            cls = "badge doable"
        elif action == "谨慎":
            cls = "badge caution"
        elif action == "放弃":
            cls = "badge avoid"
        else:
            cls = "badge"
        return f"<span class=\"{cls}\">{action or '-'}</span>"

    def _stock_cell(text: str, action: str) -> str:
        # Note: only highlight actionable "可做"
        action = str(action or "")
        extra = " stock doable" if action == "可做" else " stock"
        return f"<span class=\"{extra}\">{text} {_badge(action)}</span>"

    formation_rows = "".join(
        "<tr>"
        f"<td>{r['板块']}</td>"
        f"<td>{r['板块涨幅']}</td>"
        f"<td>{_stock_cell(r['龙头'], r.get('龙头建议', ''))}</td>"
        f"<td>{_stock_cell(r['追随1'], r.get('追随1建议', ''))}</td>"
        f"<td>{_stock_cell(r['追随2'], r.get('追随2建议', ''))}</td>"
        f"<td>{r['板块平均追随']}</td>"
        "</tr>"
        for _, r in formation_df.iterrows()
    )

    trade_date = str(meta.get("trade_date", ""))
    sm = meta.get("sector_mode") or ""
    sn = meta.get("sector_note") or ""
    sector_meta_html = ""
    if sm or sn:
        sector_meta_html = (
            '<p class="muted">板块口径：<code>'
            + html.escape(str(sm or "-"))
            + "</code>"
            + (" · " + html.escape(str(sn)) if sn else "")
            + "</p>"
        )
    perf_df = _load_performance()
    perf_hist_df = _load_performance_history()
    if not perf_df.empty:
        sample_n = len(perf_df)
        win_rate = float((perf_df["is_win"] == 1).mean())
        avg_pct = float(perf_df["close_vs_pick_close_pct"].mean())
        pick_date = str(perf_df.iloc[0]["pick_date"])
        eval_date = str(perf_df.iloc[0]["eval_date"])
        win_5d = 0.0
        if not perf_hist_df.empty:
            recent_eval_dates = sorted(perf_hist_df["eval_date"].unique())[-5:]
            recent = perf_hist_df[perf_hist_df["eval_date"].isin(recent_eval_dates)]
            if not recent.empty:
                win_5d = float((recent["is_win"] == 1).mean())
        perf_rows = "".join(
            f"<tr><td>{r['code']}</td><td>{r['name']}</td><td>{r['total_score']:.1f}</td>"
            f"<td>{r['pick_close']:.2f}</td><td>{r['eval_close']:.2f}</td>"
            f"<td>{r['close_vs_pick_close_pct']:.2f}%</td><td>{int(r['is_win'])}</td></tr>"
            for _, r in perf_df.iterrows()
        )
        perf_html = f"""
  <h2>昨日选股次日胜率（昨收 vs 今收）</h2>
  <div class="kpi compact">
    <div class="card"><b>评估区间</b><div>{pick_date} → {eval_date}</div></div>
    <div class="card"><b>样本数</b><div>{sample_n}</div></div>
    <div class="card"><b>胜率(今收&gt;昨收)</b><div>{win_rate:.1%}</div></div>
    <div class="card"><b>5日胜率</b><div>{win_5d:.1%}</div></div>
    <div class="card"><b>平均收益(今收-昨收)</b><div>{avg_pct:.2f}%</div></div>
  </div>
  <div class="table-scroll">
    <table>
      <thead><tr><th>代码</th><th>名称</th><th>评分</th><th>昨收</th><th>今收</th><th>今收-昨收%</th><th>胜出</th></tr></thead>
      <tbody>{perf_rows}</tbody>
    </table>
  </div>
"""
    else:
        perf_html = """
  <h2>昨日选股次日胜率（昨收 vs 今收）</h2>
  <p class="muted">暂无可用数据，至少运行两天后会出现（第1天存档选股，第2天评估）。</p>
"""

    page_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>盘后扫描追随｜静态仪表盘</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; margin: 20px auto; max-width: 1280px; color: #0f172a; font-size: 14px; line-height: 1.45; }}
    h1 {{ font-size: 22px; margin: 0 0 6px 0; }}
    h2 {{ font-size: 17px; margin: 22px 0 10px 0; }}
    p {{ margin: 6px 0; }}
    .muted {{ color: #64748b; }}
    .stamp {{ color: #dc2626; font-weight: 700; }}
    .kpi {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 10px 0 14px 0; }}
    .kpi.compact .card {{ min-width: 150px; }}
    .card {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 10px; min-width: 160px; background: #fff; }}
    .card b {{ font-size: 12px; color: #475569; }}
    .card div {{ font-size: 16px; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }}
    th, td {{ border: 1px solid #e2e8f0; padding: 6px 8px; text-align: left; }}
    th {{ background: #f8fafc; }}
    .table-scroll {{ max-height: 320px; overflow-y: auto; border: 1px solid #e2e8f0; border-radius: 8px; }}
    .table-scroll table {{ margin-top: 0; border: 0; }}
    .table-scroll th {{ position: sticky; top: 0; z-index: 1; }}
    code {{ background: #f1f5f9; border-radius: 4px; padding: 1px 4px; }}
    .stock {{ display: inline-flex; align-items: center; gap: 6px; padding: 2px 4px; border-radius: 6px; }}
    .stock.doable {{ background: #ecfdf5; border: 1px solid #34d399; box-shadow: inset 0 0 0 1px rgba(16,185,129,0.15); }}
    .badge {{ font-size: 12px; padding: 1px 6px; border-radius: 999px; border: 1px solid #e2e8f0; background: #f8fafc; color: #475569; }}
    .badge.doable {{ background: #d1fae5; border-color: #34d399; color: #065f46; }}
    .badge.caution {{ background: #fffbeb; border-color: #fbbf24; color: #92400e; }}
    .badge.avoid {{ background: #fef2f2; border-color: #f87171; color: #991b1b; }}
  </style>
</head>
<body>
  <h1>盘后扫描追随｜静态仪表盘</h1>
  <p class="muted">生成时间：<span class="stamp">{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</span> | 交易日：{trade_date or "-"}</p>
  {sector_meta_html}
  <div class="kpi">
    <div class="card"><b>情绪</b><div>{emotion}</div></div>
    <div class="card"><b>仓位建议</b><div>{position_advice}</div></div>
    <div class="card"><b>最高连板</b><div>{market.max_board_height}</div></div>
    <div class="card"><b>炸板率</b><div>{market.blowup_rate:.1%}</div></div>
  </div>
  <p><b>核心原则：</b>{principle}</p>
  <h2>板块队形（龙头 -> 追随）</h2>
  <table>
    <thead><tr><th>板块</th><th>板块涨幅(%)</th><th>龙头</th><th>追随1</th><th>追随2</th><th>板块平均追随(1-10)</th></tr></thead>
    <tbody>{formation_rows}</tbody>
  </table>
  {perf_html}
</body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(page_html, encoding="utf-8")
    return output_path
