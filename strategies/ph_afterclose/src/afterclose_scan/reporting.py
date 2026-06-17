from __future__ import annotations

from datetime import datetime

from .models import SectorSnapshot, StockScore


DISCLAIMER = (
    "数据基于 Tushare 收盘行情，仅为量化模型推演，不构成交易建议，"
    "入市有风险，投资需谨慎。"
)


def _pick_diversified(scores: list[StockScore], n: int, max_per_sector: int = 2) -> list[StockScore]:
    ranked = sorted(scores, key=lambda x: (x.follow_score, x.total_score), reverse=True)
    counts: dict[str, int] = {}
    out: list[StockScore] = []
    for s in ranked:
        if counts.get(s.sector, 0) >= max_per_sector:
            continue
        out.append(s)
        counts[s.sector] = counts.get(s.sector, 0) + 1
        if len(out) >= max(1, n):
            break
    if len(out) < max(1, n):
        fallback = [s for s in ranked if s not in out]
        out.extend(fallback[: max(1, n) - len(out)])
    return out[: max(1, n)]


def render_report(
    emotion: str,
    position_advice: str,
    principle: str,
    top_sectors: list[SectorSnapshot],
    scores: list[StockScore],
    minimal_n: int = 5,
) -> str:
    lines: list[str] = []
    lines.append(f"# 盘后扫描追随｜次日作战计划（{datetime.now().strftime('%Y-%m-%d %H:%M')}）")
    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")
    lines.append("## 第一步：市场情绪温度计")
    lines.append(f"- 情绪判定：**{emotion}**")
    lines.append(f"- 总仓位建议：**{position_advice}**")
    lines.append(f"- 次日核心原则：{principle}")
    lines.append("")
    lines.append("## 第二步：主线板块定位（按今日强度）")
    for idx, sec in enumerate(top_sectors, start=1):
        lines.append(
            f"- TOP{idx} `{sec.name}` | 板块涨幅 {sec.pct_chg:.2f}% | "
            f"持续性 {sec.persistence_score:.1f}/10 | 阶段 {sec.stage}"
        )
    lines.append("")
    lines.append("## 第三步&第四步：龙头阵营与跟随候选")
    lines.append("- 以下按综合分从高到低：")
    for s in sorted(scores, key=lambda x: x.total_score, reverse=True):
        star = f"（{'/'.join(s.tags)}）" if s.tags else ""
        lines.append(
            f"- `{s.name}({s.code})` 总分 **{s.total_score}/40** {star} | "
            f"量能{ s.score_liquidity } 位置{ s.score_price_position } "
            f"题材{ s.score_theme_status } 人气{ s.score_popularity } 追随{ s.follow_score } "
            f"安全{ s.anti_chase_score } 建议{ s.action }"
        )
        lines.append(f"  - 优点：{s.pros}")
        lines.append(f"  - 风险：{s.risks}")
    lines.append("")
    lines.append("## 次日重点关注（少而精）")
    focus = [s for s in scores if s.follow_score >= 8][: max(1, minimal_n)]
    if not focus:
        focus = sorted(scores, key=lambda x: (x.follow_score, x.total_score), reverse=True)[: max(1, minimal_n)]
    for s in focus:
        lines.append(
            f"- `{s.name}({s.code})` | 总分 {s.total_score}/40 | 追随 {s.follow_score}/10 | "
            f"{' / '.join(s.tags) if s.tags else '观察'}"
        )
    lines.append("")
    lines.append("## 防追高过滤（次日风险）")
    doable = [s for s in scores if s.action == "可做"][: max(1, minimal_n)]
    caution = [s for s in scores if s.action == "谨慎"][: max(1, minimal_n)]
    avoid = [s for s in scores if s.action == "放弃"][: max(1, minimal_n)]
    lines.append("- 可做：")
    for s in doable:
        lines.append(f"  - `{s.name}({s.code})` 安全分{s.anti_chase_score}/10 追随{s.follow_score}/10")
    lines.append("- 谨慎：")
    for s in caution:
        lines.append(f"  - `{s.name}({s.code})` 安全分{s.anti_chase_score}/10 追随{s.follow_score}/10")
    lines.append("- 放弃：")
    for s in avoid:
        lines.append(f"  - `{s.name}({s.code})` 安全分{s.anti_chase_score}/10 追随{s.follow_score}/10")
    lines.append("")
    lines.append("## 第五步：策略与风险预案")
    lines.append("- 理想介入：次日不跌破前收并守住分时均线，板块龙头维持强势。")
    lines.append("- 放弃条件：高开杀绿、龙头被核、板块强度衰减。")
    lines.append("- 风险提示：**任何模型在极端情绪日都会失真，先活下来，再追求收益。**")
    lines.append("")
    return "\n".join(lines)
