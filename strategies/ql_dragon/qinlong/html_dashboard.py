"""生成静态 HTML 仪表盘（离线、可本地打开）。"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from qinlong.strategy_policy import strategy_bucket


def _esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _fmt(x: Any, nd: int = 2) -> str:
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return _esc(x)


def _tag_badges(row: pd.Series) -> str:
    tags: list[str] = []
    if bool(row.get("from_limit_list")):
        tags.append('<span class="pill pill-red">涨停</span>')
    if bool(row.get("from_hm_detail")):
        tags.append('<span class="pill pill-blue">龙虎榜</span>')
    if (row.get("concept_tags") or "").strip():
        tags.append('<span class="pill pill-gray">题材映射</span>')
    return " ".join(tags)


def _structure_hint(row: pd.Series) -> str:
    ratio = row.get("close_to_prev_high")
    volr = row.get("vol_to_ma5")
    limit_up = row.get("limit_up_days")
    turnover = row.get("turnover_rate")
    mv = row.get("circ_mv_yi")

    parts: list[str] = []
    try:
        if pd.notna(limit_up) and float(limit_up) > 0:
            parts.append(f"{int(float(limit_up))}连板")
    except Exception:
        pass

    try:
        if ratio is not None and not pd.isna(ratio):
            r = float(ratio)
            if r >= 1.03:
                parts.append("已突破前高参考带")
            elif r >= 1.0:
                parts.append("贴近前高参考带")
            elif r >= 0.95:
                parts.append("蓄势区")
            else:
                parts.append("仍在压力下")
    except Exception:
        pass
    try:
        if volr is not None and not pd.isna(volr):
            v = float(volr)
            if v >= 2.0:
                parts.append("爆量")
            elif v >= 1.5:
                parts.append("放量")
            elif v >= 1.0:
                parts.append("量能温和")
            else:
                parts.append("量能偏弱")
    except Exception:
        pass

    extra: list[str] = []
    try:
        if turnover is not None and not pd.isna(turnover):
            extra.append(f"换手{float(turnover):.1f}%")
        if mv is not None and not pd.isna(mv):
            extra.append(f"市值{float(mv):.0f}亿")
    except Exception:
        pass

    res = " / ".join(parts) if parts else "综合入围"
    if extra:
        res += f" ({', '.join(extra)})"
    return res


def _action_suggestion(row: pd.Series) -> str:
    tech = float(row.get("s_technical") or 0.0)
    limit_up = row.get("limit_up_days")
    close = row.get("close")
    ema_5 = row.get("ema_5")
    
    # 构造具体价格建议
    price_hint = ""
    try:
        if pd.notna(close) and pd.notna(ema_5):
            c = float(close)
            e5 = float(ema_5)
            # 五日线附近是常见低吸点
            if c >= e5:
                price_hint = f"建议在5日线（{e5:.2f}附近）低吸；"
            else:
                price_hint = f"已破5日线（{e5:.2f}），建议观望或沿10日线低吸；"
    except Exception:
        pass

    if not price_hint:
        price_hint = "不追高；等回踩前高转支撑或缩量整理后的再放量。"

    try:
        if pd.notna(limit_up) and float(limit_up) >= 2:
            return price_hint + "高位连板：注意情绪分歧，不建议直线追高，博弈首阴回踩。"
    except Exception:
        pass

    if tech >= 6:
        return price_hint + "结构偏强：等分歧回调或缩量整理再介入。"
    return price_hint + "题材/情绪可能更强，先等结构共振。"


def render_dashboard_html(
    df: pd.DataFrame,
    *,
    trade_date: str,
    throttle: dict[str, Any] | None = None,
    universe_note: str = "涨停 + 同花顺热榜题材映射（若当日热榜为空则退化为涨停候选），剔除 ST",
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    throttle_line = _esc(throttle) if throttle else ""

    rows_html: list[str] = []
    for _, r in df.iterrows():
        label, css, strat_note = strategy_bucket(r)
        rows_html.append(
            "<tr>"
            f"<td class='num'>{_fmt(r.get('score'))}</td>"
            f"<td class='strat'><span class='strat-tag {css}'>{_esc(label)}</span>"
            f"<div class='strat-note'>{_esc(strat_note)}</div></td>"
            f"<td><div class='code'>{_esc(r.get('ts_code'))}</div><div class='badges'>{_tag_badges(r)}</div></td>"
            f"<td><div class='name'>{_esc(r.get('name'))}</div><div class='sub'>{_esc(r.get('industry'))}</div></td>"
            f"<td><div class='hint'>{_esc(_structure_hint(r))}</div><div class='sub'>{_esc(r.get('concept_tags') or '')}</div></td>"
            f"<td class='num score-detail'>{_fmt(r.get('s_theme'))}</td>"
            f"<td class='num score-detail'>{_fmt(r.get('s_news'))}</td>"
            f"<td class='num score-detail'>{_fmt(r.get('s_technical'))}</td>"
            f"<td class='num score-detail'>{_fmt(r.get('s_cap_turnover'))}</td>"
            f"<td class='num score-detail'>{_fmt(r.get('s_fundamental'))}</td>"
            f"<td class='num score-detail'>{_fmt(r.get('s_chip'))}</td>"
            f"<td class='advice'>{_esc(_action_suggestion(r))}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>擒龙猎手 · 扫描仪表盘 {trade_date}</title>
  <style>
    :root {{
      --bg: #0b0f14;
      --panel: #101826;
      --text: #e6edf3;
      --muted: #9fb0c0;
      --line: #223044;
      --accent: #4ea1ff;
      --red: #ff5c5c;
      --blue: #5cc8ff;
      --gray: #6b7f93;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"PingFang SC\", \"Hiragino Sans GB\", \"Microsoft YaHei\", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 18px 16px 30px; }}
    h1 {{ font-size: 20px; margin: 0 0 6px; }}
    .meta {{ color: var(--muted); font-size: 12px; line-height: 1.6; }}
    .bar {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }}
    .stat {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }}
    .stat .k {{ color: var(--muted); font-size: 12px; }}
    .stat .v {{ margin-top: 4px; font-weight: 700; font-size: 16px; }}
    .callout {{ background: rgba(255, 92, 92, 0.08); border: 1px solid rgba(255, 92, 92, 0.25); border-radius: 10px; padding: 10px 12px; color: var(--text); }}
    .callout b {{ color: var(--red); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 8px; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: rgba(11, 15, 20, 0.96); backdrop-filter: blur(6px); text-align: left; font-size: 12px; color: var(--muted); }}
    td {{ font-size: 13px; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace; font-size: 12px; }}
    .name {{ font-weight: 700; }}
    .sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .hint {{ color: var(--text); }}
    .advice {{ max-width: 320px; color: var(--text); }}
    .badges {{ margin-top: 6px; }}
    .pill {{ display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 11px; border: 1px solid var(--line); margin-right: 6px; }}
    .pill-red {{ border-color: rgba(255,92,92,0.35); color: var(--red); background: rgba(255,92,92,0.08); }}
    .pill-blue {{ border-color: rgba(92,200,255,0.35); color: var(--blue); background: rgba(92,200,255,0.08); }}
    .pill-gray {{ border-color: rgba(107,127,147,0.35); color: #c9d7e5; background: rgba(107,127,147,0.10); }}
    .strat {{ min-width: 120px; }}
    .strat-tag {{ display: inline-block; font-weight: 700; font-size: 12px; padding: 4px 10px; border-radius: 8px; border: 1px solid var(--line); }}
    .strat-note {{ margin-top: 6px; font-size: 11px; color: var(--muted); line-height: 1.45; max-width: 200px; }}
    .strat-go {{ color: #7ee787; border-color: rgba(126,231,135,0.35); background: rgba(126,231,135,0.10); }}
    .strat-watch {{ color: var(--accent); border-color: rgba(78,161,255,0.35); background: rgba(78,161,255,0.08); }}
    .strat-warn {{ color: #f0c674; border-color: rgba(240,198,116,0.35); background: rgba(240,198,116,0.08); }}
    .strat-drop {{ color: #8b949e; border-color: rgba(139,148,158,0.35); background: rgba(139,148,158,0.08); }}
    .legend {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin-top: 12px; font-size: 12px; color: var(--muted); line-height: 1.7; }}
    .legend strong {{ color: var(--text); }}
    .foot {{ margin-top: 14px; color: var(--muted); font-size: 12px; }}
    .hide-details .score-detail {{ display: none; }}
    .toggle-btn {{ margin-top: 12px; background: rgba(78,161,255,0.10); color: var(--accent); border: 1px solid rgba(78,161,255,0.25); padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.2s; }}
    .toggle-btn:hover {{ background: rgba(78,161,255,0.20); }}
    @media (max-width: 900px) {{
      .bar {{ grid-template-columns: 1fr; }}
      .score-detail {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>擒龙猎手 · 扫描仪表盘</h1>
    <div class="meta">
      交易日：<b>{_esc(trade_date)}</b> · 生成时间：{_esc(now)}<br/>
      Universe：{_esc(universe_note)}<br/>
      Throttle：{throttle_line}
    </div>
    <div class="bar">
      <div class="stat"><div class="k">标的数</div><div class="v">{len(df)}</div></div>
      <div class="stat"><div class="k">最高综合分</div><div class="v">{_esc(_fmt(df['score'].max() if not df.empty and 'score' in df.columns else ''))}</div></div>
      <div class="stat"><div class="k">提示</div><div class="v" style="color: var(--accent); font-size: 13px;">右侧优先：突破后等回踩/缩量再放量</div></div>
    </div>
    <div class="callout">
      <b>声明：</b>本页为研究与复盘工具，不构成投资建议。请结合自身风险偏好与交易纪律执行。
    </div>
    <div class="legend">
      <strong>操作策略（粗筛）</strong><br/>
      <span class="strat-tag strat-go">上车</span> 题材/技术/综合同时偏强，仅允许<strong>小仓试错</strong>，仍需等回踩或缩量再起；假突破参考跌回前高下约3%。<br/>
      <span class="strat-tag strat-watch">观察</span> 先<strong>自选跟踪</strong>，等共振或驭龙点再加。<br/>
      <span class="strat-tag strat-warn">谨慎</span> 叙事与走势<strong>错位</strong>或筹码分歧大，宁可错过。<br/>
      <span class="strat-tag strat-drop">废弃</span> 本轮<strong>剔除观察</strong>，不占注意力。
    </div>
    
    <button class="toggle-btn" onclick="document.body.classList.toggle('hide-details')">👁️ 切换打分明细</button>

    <table>
      <thead>
        <tr>
          <th class="num">综合</th>
          <th>策略</th>
          <th>代码/标签</th>
          <th>名称/行业</th>
          <th>结构与题材</th>
          <th class="num score-detail">题材</th>
          <th class="num score-detail">热度</th>
          <th class="num score-detail">技术</th>
          <th class="num score-detail">市换</th>
          <th class="num score-detail">基本面</th>
          <th class="num score-detail">筹码</th>
          <th>操作建议</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>

    <div class="foot">
      分项口径：题材=热榜板块映射+涨停加成；热度=大单/龙虎榜代理；技术=前高参考+量能+均线/MACD/KDJ；基本面=增速/ROE粗分；筹码=胜率区间。<br/>
      若当日热榜为空：题材会退化为低分（仅涨停加成），建议改用指定日期或启用其它题材源补全。
    </div>
  </div>
</body>
</html>
"""


def write_dashboard_html(
    df: pd.DataFrame,
    out_path: Path,
    *,
    trade_date: str,
    throttle: dict[str, Any] | None = None,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_dashboard_html(df, trade_date=trade_date, throttle=throttle)
    out_path.write_text(html_text, encoding="utf-8")
    return out_path

