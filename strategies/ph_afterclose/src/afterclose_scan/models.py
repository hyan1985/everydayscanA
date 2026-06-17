from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


EmotionLevel = Literal["冰点", "低迷", "回暖", "高潮", "分歧"]
Stage = Literal["发酵", "加速", "分歧", "反抽", "一日游"]


@dataclass
class MarketSnapshot:
    max_board_height: int
    promotion_rate: float
    blowup_rate: float
    index_pct_chg: float = 0.0
    median_pct_chg: float = 0.0


@dataclass
class SectorSnapshot:
    name: str
    pct_chg: float
    persistence_score: float
    stage: Stage


@dataclass
class StockSnapshot:
    name: str
    code: str
    sector: str
    pct_chg: float
    turnover_rate: float
    volume_ratio: float
    amount: float
    float_mkt_cap_billion: float
    position_tag: Literal["低位首板", "二板确认", "一致后加速", "高位"]
    is_sector_leader: bool
    has_catalyst: bool
    popularity_score: float = 5.0
    is_limit_up: bool = False
    has_limit_up_leader_in_sector: bool = False
    follow_rank_in_sector: int = 99
    leader_pct_chg: float = 0.0


@dataclass
class StockScore:
    name: str
    code: str
    sector: str
    score_liquidity: float
    score_price_position: float
    score_theme_status: float
    score_popularity: float
    follow_score: float
    anti_chase_score: float
    action: Literal["可做", "谨慎", "放弃"]
    total_score: float
    pros: str
    risks: str
    tags: list[str] = field(default_factory=list)
