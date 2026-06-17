"""统一概念管理器 — 跨项目共享主题配置。

用法：
    from quant_data.concepts import load_concepts
    c = load_concepts()
    c.get_themes()      # ["智能电网", "储能", ...]
    c.get_aliases()     # {"智能电网": [...], ...}
    c.match_concepts(...)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import yaml


class ConceptManager:
    """统一概念管理器。

    支持注入当日热榜板块名称，让策略的候选池自动包含热榜板块成分股。
    """

    def __init__(self, config: dict):
        self._config = config
        self._themes: list[str] = config.get("themes", [])
        self._aliases: dict[str, list[str]] = config.get("aliases", {})
        # 热榜板块作为临时主题，注入到关键词中
        self._hot_board_themes: list[str] = []
        self._hot_board_names: list[str] = []

    def inject_hot_boards(self, board_names: list[str]):
        """注入今日热榜前 N 的板块名称。

        这些名称会参与 get_all_keywords() 返回的关键词匹配，
        使策略能自动选中热榜板块的成分股，无需手动配置。
        """
        self._hot_board_names = board_names
        self._hot_board_themes = board_names[:]

    def get_themes(self) -> list[str]:
        result = self._themes[:] if self._themes else self._config.get("fifteenth_five_themes", [])
        # 热榜板块也作为临时 themes
        if self._hot_board_themes:
            for t in self._hot_board_themes:
                if t not in result:
                    result.append(t)
        return result

    def get_aliases(self) -> dict[str, list[str]]:
        return self._aliases

    def get_theme_names(self) -> list[str]:
        return list(self.get_aliases().keys()) or self.get_themes()

    def match_concepts(self, concept_text: str) -> list[str]:
        """检查一段概念文本匹配哪些主题，返回匹配的主题名列表。"""
        text = concept_text.lower()
        matched = []
        for theme, aliases in self._aliases.items():
            for alias in aliases:
                if alias.lower() in text:
                    matched.append(theme)
                    break
        return matched

    def get_all_keywords(self) -> list[str]:
        """返回所有别名关键词 + 热榜板块名，用于 THS 搜索。"""
        kws = set()
        for aliases in self._aliases.values():
            for a in aliases:
                if a:
                    kws.add(a)
        # 注入热榜板块名
        for name in self._hot_board_names:
            if name:
                kws.add(name)
        return sorted(kws)

    def to_old_json_format(self) -> dict:
        """以旧 themes.json 格式输出（兼容旧代码过渡期）。"""
        return {
            "fifteenth_five_themes": self.get_themes(),
            "aliases": self.get_aliases(),
        }


def load_concepts(
    path: Optional[str] = None,
    fallback_json: Optional[str] = None,
    inject_hot_boards: bool = False,
) -> ConceptManager:
    """加载概念配置。

    优先级：
    1. 显式传入的 path
    2. 环境变量 QUANT_CONCEPTS_PATH
    3. 统一默认 config/concepts.yaml
    4. 回退到旧 themes.json（fallback_json 参数）
    """
    p = path or os.environ.get("QUANT_CONCEPTS_PATH", "")

    if not p:
        p = str(Path(__file__).resolve().parent.parent / "config" / "concepts.yaml")

    if Path(p).exists():
        with open(p, encoding="utf-8") as f:
            if p.endswith(".yaml") or p.endswith(".yml"):
                config = yaml.safe_load(f)
            else:
                config = json.load(f)
        mgr = ConceptManager(config)
        if inject_hot_boards:
            # 尝试注入当日热榜前 15 板块
            try:
                from datetime import datetime
                td = datetime.now().strftime("%Y%m%d")
                names = get_hot_board_names(td, top_n=15)
                if names:
                    mgr.inject_hot_boards(names)
            except Exception:
                pass
        return mgr

    # 回退旧格式
    if fallback_json and Path(fallback_json).exists():
        with open(fallback_json, encoding="utf-8") as f:
            config = json.load(f)
        return ConceptManager(config)

    # 空管理器
    return ConceptManager({})


def write_local_themes_json(project_dir: str, out_name: str = "config/themes.json"):
    """从统一 concepts.yaml 生成旧 themes.json（供未改造前的策略读取）。"""
    c = load_concepts()
    dst = Path(project_dir) / out_name
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(c.to_old_json_format(), f, ensure_ascii=False, indent=2)
    return dst


# ── 热榜板块补充 ──────────────────────────────────────────

_HOT_BOARD_CODES_CACHE: dict[str, set[str]] = {}
_HOT_BOARD_HOT_TS_CODES: set[str] = set()


def get_hot_board_stocks(trade_date: str, top_n: int = 15) -> set[str]:
    """获取同花顺热榜前 top_n 板块的所有成分股代码（去重）。

    底层套用 DataProvider 走本地 Parquet 缓存的 ths_hot / ths_member，
    仅为减少重复 API 调用做了 memo 缓存。
    """
    if trade_date in _HOT_BOARD_CODES_CACHE:
        return _HOT_BOARD_CODES_CACHE[trade_date]

    from quant_data import get_provider
    pro = get_provider()

    codes: set[str] = set()
    try:
        hot = pro.ths_hot(trade_date=trade_date)
        if hot is not None and not hot.empty:
            hot = hot[hot["data_type"].isin(["概念板块", "行业板块"])]
            hot = hot.sort_values("rank").head(top_n)
            _HOT_BOARD_HOT_TS_CODES.update(hot["ts_code"].tolist())
            for _, row in hot.iterrows():
                idx = row["ts_code"]
                if not isinstance(idx, str) or not idx.endswith(".TI"):
                    continue
                try:
                    memb = pro.ths_member(ts_code=idx)
                    if memb is not None and not memb.empty:
                        codes.update(memb["con_code"].astype(str).tolist())
                except Exception:
                    continue
    except Exception:
        pass

    codes = {c for c in codes if not c.endswith(".BJ")}
    _HOT_BOARD_CODES_CACHE[trade_date] = codes
    return codes


def get_hot_board_names(trade_date: str, top_n: int = 15) -> list[str]:
    """获取今日热榜前 top_n 的板块名称列表。"""
    from quant_data import get_provider
    pro = get_provider()
    names: list[str] = []
    try:
        hot = pro.ths_hot(trade_date=trade_date)
        if hot is not None and not hot.empty:
            hot = hot[hot["data_type"].isin(["概念板块", "行业板块"])]
            hot = hot.sort_values("rank").head(top_n)
            # ths_hot 的板块名称列是 ts_name
            names = hot["ts_name"].tolist()
    except Exception:
        pass
    return names
