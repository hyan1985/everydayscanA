from __future__ import annotations

from .models import EmotionLevel, MarketSnapshot, SectorSnapshot, StockScore, StockSnapshot


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def evaluate_emotion(m: MarketSnapshot) -> tuple[EmotionLevel, str, str]:
    if m.max_board_height <= 2 and m.promotion_rate < 0.35 and m.blowup_rate > 0.35:
        return "冰点", "空仓休息", "只做观察，不做主观抄底。"
    if m.max_board_height <= 3 and m.promotion_rate < 0.45:
        return "低迷", "1成以下试错", "只看最强辨识度，不碰后排。"
    if m.max_board_height >= 5 and m.promotion_rate >= 0.55 and m.blowup_rate <= 0.22:
        return "高潮", "3-5成积极", "顺主线做龙头，拒绝杂毛。"
    if m.blowup_rate >= 0.28 and m.promotion_rate >= 0.48:
        return "分歧", "去弱留强", "只做分歧转一致，不做追高接力。"
    return "回暖", "1-2成试错", "先试错前排，确认后再加仓。"


def pick_top_sectors(sectors: list[SectorSnapshot], n: int = 3) -> list[SectorSnapshot]:
    return sorted(
        sectors,
        key=lambda x: (x.pct_chg * 0.6 + x.persistence_score * 0.4),
        reverse=True,
    )[:n]


def score_stock(stock: StockSnapshot, strongest_sectors: set[str]) -> StockScore:
    # 量能：以换手率 + 量比组合衡量盘后真实活跃度
    turnover = stock.turnover_rate
    volr = stock.volume_ratio
    if 4.0 <= turnover <= 12.0:
        liq = 8.5
    elif 2.5 <= turnover < 4.0 or 12.0 < turnover <= 18.0:
        liq = 6.8
    else:
        liq = 4.5
    if volr >= 2.0:
        liq += 1.0
    elif volr >= 1.3:
        liq += 0.5
    elif volr < 0.8:
        liq -= 0.8
    liq = _clamp(liq, 1.0, 10.0)

    # 位置：以今日涨幅 + 流通市值 + 位置标签
    pct = stock.pct_chg
    if 3 <= pct <= 7:
        price_pos = 8.5
    elif 1 <= pct < 3 or 7 < pct <= 9:
        price_pos = 6.8
    else:
        price_pos = 4.8
    if 20 <= stock.float_mkt_cap_billion <= 100:
        price_pos += 1.0
    if stock.position_tag in {"低位首板", "二板确认"}:
        price_pos += 0.6
    elif stock.position_tag == "高位":
        price_pos -= 0.8
    price_pos = _clamp(price_pos, 1.0, 10.0)

    # 题材
    theme = 5.5
    if stock.sector in strongest_sectors:
        theme += 2.2
    if stock.is_sector_leader:
        theme += 1.5
    if stock.has_catalyst:
        theme += 1.0
    theme = _clamp(theme, 1.0, 10.0)

    pop = _clamp(stock.popularity_score, 1.0, 10.0)

    # 追随分：板块内是否存在涨停龙头 + 自身在板块中的位次
    follow = 3.5
    if stock.has_limit_up_leader_in_sector:
        follow += 2.5
    if stock.follow_rank_in_sector <= 2:
        follow += 2.0
    elif stock.follow_rank_in_sector <= 4:
        follow += 1.0
    if stock.leader_pct_chg >= 9.5:
        follow += 1.0
    elif stock.leader_pct_chg >= 7.0:
        follow += 0.5
    if stock.is_limit_up:
        # 已经涨停的票本身不再适合作为"追随对象"
        follow -= 1.2
    follow = _clamp(follow, 1.0, 10.0)

    # 防追高分：今日涨幅越大、本身已涨停、追随位次靠后都拉低安全分。
    # 前排「结构化追随」：板内有涨停锚、且为入围前排时，对追随与「涨停龙一」都给缓释（避免 tie-break 换龙一后建议档位乱跳）。
    anti = 7.0
    if pct >= 8.5:
        anti -= 2.5
    elif pct >= 6.0:
        anti -= 1.5
    elif pct >= 4.0:
        anti -= 0.6
    # 收盘涨停对「次日接力」的扣分：龙一/追随同一档。龙一若再用 -2.5，会与下方「仅非龙头才缓释」
    # 叠加，在「并列涨停改 tie-break 龙一」时会把同一只票从可做打成放弃（量化龙一≠更该恐慌）。
    if stock.is_limit_up:
        anti -= 1.2
    if stock.follow_rank_in_sector >= 5:
        anti -= 1.0
    if stock.position_tag == "高位":
        anti -= 0.8
    if turnover >= 18.0:
        anti -= 0.6
    # 板内有涨停锚时的前排结构化缓释：追随票；或「涨停龙一」且仍在入围前排（避免 tie-break 换龙一后少一大截安全分）
    if (
        stock.has_limit_up_leader_in_sector
        and stock.follow_rank_in_sector <= 4
        and (not stock.is_sector_leader or stock.is_limit_up)
    ):
        anti += 2.2
        if stock.follow_rank_in_sector <= 2:
            anti += 0.8
    anti = _clamp(anti, 1.0, 10.0)

    total = round(liq + price_pos + theme + pop, 1)
    tags: list[str] = []
    if total >= 36:
        tags.append("五星核心")
    elif total >= 32:
        tags.append("四星潜力")
    if follow >= 8:
        tags.append("追随优先")
    if anti <= 4.5:
        tags.append("防追高警示")

    # 追随策略里大阳票多，阈值略放宽：前排追随 + 安全分尚可即可落到谨慎/可做
    if anti >= 6.2 and follow >= 6.3:
        action = "可做"
    elif anti >= 4.8:
        action = "谨慎"
    else:
        action = "放弃"

    follow_text = (
        f"板块涨停龙头存在，追随位次{stock.follow_rank_in_sector}"
        if stock.has_limit_up_leader_in_sector
        else "板块缺涨停锚，追随逻辑较弱"
    )
    pros = f"换手{turnover:.1f}% 量比{volr:.2f}，题材匹配度高，{follow_text}。"
    risks = "若次日高开冲高遇阻或放量破均线，强转弱风险高。"
    return StockScore(
        name=stock.name,
        code=stock.code,
        sector=stock.sector,
        score_liquidity=round(liq, 1),
        score_price_position=round(price_pos, 1),
        score_theme_status=round(theme, 1),
        score_popularity=round(pop, 1),
        follow_score=round(follow, 1),
        anti_chase_score=round(anti, 1),
        action=action,
        total_score=total,
        pros=pros,
        risks=risks,
        tags=tags,
    )
