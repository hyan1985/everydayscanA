"""粗粒度操作策略：上车 / 观察 / 谨慎 / 废弃（与 HTML、Canvas 快照共用）。"""

from __future__ import annotations

import pandas as pd


def strategy_bucket(row: pd.Series) -> tuple[str, str, str]:
    """
    返回：(标签文案, HTML/CSS 类名, 一行说明)
    口径为复盘筛选用，不构成投资建议。
    """
    score = float(row.get("score") or 0.0)
    theme = float(row.get("s_theme") or 0.0)
    tech = float(row.get("s_technical") or 0.0)
    chip = float(row.get("s_chip") or 5.0)

    if score < 2.8:
        return "废弃", "strat-drop", "综合分过低，从自选剔除或不再跟踪。"
    if tech < 2.0 and theme < 3.0:
        return "废弃", "strat-drop", "题材与技术均无共振，不占注意力。"

    if score >= 5.5 and tech >= 5.5 and theme >= 7.5:
        return "上车", "strat-go", "题材+技术+分数同时偏强：仅计划试错仓，等回踩或缩量再起。"
    if score >= 5.3 and tech >= 6.5:
        return "上车", "strat-go", "动能结构突出：分批思路，避免直线追高。"

    if theme >= 9.0 and tech < 4.5:
        return "谨慎", "strat-warn", "题材极端热、图形未完全跟上，防情绪末端。"
    if tech >= 6.5 and theme < 4.0:
        return "谨慎", "strat-warn", "图形偏强、题材映射弱，独立性行情波动大。"
    if chip >= 8.5 and tech < 5.0:
        return "谨慎", "strat-warn", "筹码获利偏高，分歧与波动可能放大。"

    if score >= 3.5:
        return "观察", "strat-watch", "可放自选：等三位一体再收紧或出现驭龙点再加仓。"

    return "废弃", "strat-drop", "性价比一般，本轮不作为重点。"
