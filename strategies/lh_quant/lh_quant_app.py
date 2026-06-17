"""
量化蓄势突破（策略核心实现）

说明：
- `searchv1.py` 保留为入口/兼容层（run_all.sh / scripts/run_daily.sh 依赖它的导入路径）
- 本文件承载主要实现，便于后续模块化拆分与性能优化
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# box_range_monitor 兼容：保持与旧入口一致
sys.path.append(os.path.join(os.path.dirname(__file__), "box_range_monitor", "src"))
try:
    from box_range_monitor.signals import detect_box_signal
except ImportError:
    detect_box_signal = None

from execution_policy import dynamic_decay_levels, dynamic_decay_plan_texts


DEFAULT_THEME_KEYWORDS = [
    "新型电力系统", "储能", "电网", "特高压",  # 能源
    "工业母机", "工业机器人", "智能制造",  # 制造
    "AIGC", "人工智能", "算力", "半导体",  # 数字/科技
    "CPO", "PCB", "光通信", "光模块",  # 算力硬件
    "低空经济", "无人机", "商业航天",  # 新质生产力
    "新材料概念", "合成生物", "生物制造",  # 前沿技术
    "创新药", "医疗器械概念",  # 健康
    "物理AI", "算力网",  # AI 基础设施
    "长鑫存储", "长江存储",  # 国产存储
]

DEFAULT_RISK_CONFIG = {
    "volatility_30d_high": 0.55,
    "turnover_rate_low": 0.6,
    "turnover_rate_high": 12.0,
    "breakout_readiness_chase": 92.0,
    "stop_loss_pct": 0.08,
    "take_profit_pct": 0.18,
    "max_runup_10d_pct": 18.0,
    "max_near_high_20d_ratio": 0.985,
    "concept_strength_keep_ratio": 0.5,
    "min_accumulation_score": 55.0,
    "box_low_position_min": 0.35,
    "box_low_position_max": 0.75,
    "min_setup_candidates": 8,
}

DEFAULT_VALUATION_CONFIG = {
    "valuation_candidate_top_n": 80,
    "valuation_weight_total_score": 0.9,
    "valuation_weight_5y": 0.1,
    "valuation_weight_3y_confirm": 0.0,
    "concept_rotation_weight": 0.1,
    "bull_valuation_scale": 0.35,
    "bull_bypass_enabled": 1,
    "bull_bypass_momentum_20d_min": 0.08,
    "v4_valuation_weight": 0.05,
    "v4_strong_momentum_bypass": 1,
    "v4_bypass_momentum_20d_min": 0.06,
}

DEFAULT_STRATEGY_CONFIG = {
    "mode": "v4_pure",
}

DEFAULT_TRADE_CONFIG = {
    "max_daily_picks": 5,
    "entry_breakout_buffer_pct": 0.2,
    "entry_need_above_ma5": 1.0,
    "entry_min_ma5_slope_5d": 0.0,
    "setup_volume_ratio_min": 1.2,
    "setup_amount_ratio_min": 1.3,
    "hard_stop_loss_pct": 0.06,
    "tp1_pct": 0.08,
    "tp2_pct": 0.15,
}

DEFAULT_CROSS_FLOW_CONFIG = {
    "enabled": 1,
    "pre_cross_lookback_days": 3,
    "pre_cross_gap_shrink_ratio_max": 0.6,
    "pre_cross_gap_pct_max": 0.008,
    "pre_cross_gap_near_min_ratio": 1.15,
    "pre_cross_gap_near_min_window": 8,
    "is_pre_cross_shrink_ratio_max": 0.9,
    "wr_period_fast": 14,
    "wr_period_slow": 28,
    "wr_down_days_min": 2,
    "wr_fast_max": 55.0,
    "wr_trend_score_min": 58.0,
    "moneyflow_lookback_days": 3,
    "moneyflow_net_amount_rate_min": 6.0,
    "moneyflow_positive_days_min": 2,
    "top_inst_lookback_days": 5,
    "top_inst_net_buy_min_wan": 3000.0,
    "bull_min_rotation_score": 45.0,
    "range_min_rotation_score": 55.0,
    "bear_min_rotation_score": 65.0,
    "bull_allow_pre_cross": 1,
    "range_allow_pre_cross": 0,
    "bear_allow_pre_cross": 0,
    "bear_enable_trading": 0,
    "pre_cross_box_position_max": 0.45,
    "pre_cross_volume_ratio_min": 0.9,
    "pre_cross_volume_ratio_max": 1.4,
    "pre_cross_flow_slope_min": 0.0,
    "pre_cross_no_price_flow_divergence": 1,
    "just_cross_lookback_days": 6,
    "just_cross_max_age_days": 2,
    "macd_hist_slope_min": 0.0,
    "macd_hist_expand_min_days": 2,
    "require_cross_timing_for_entry": 1,
    "rotation_entry_hard_gate": 0,
    "rotation_pool_hard_gate": 0,
    "rotation_extreme_weak_floor": -1.0,
}

OUTPUT_COL_CN_MAP = {
    "ts_code": "股票代码",
    "name": "股票名称",
    "board": "板块",
    "industry": "行业",
    "concept_name": "概念",
    "latest_trade_date": "交易日期",
    "latest_close": "最新价",
    "entry_trigger_price": "触发价",
    "stop_loss_price": "止损参考价",
    "take_profit_price": "止盈参考价",
    "hard_stop_loss_price": "硬止损价",
    "tp1_price": "止盈一档价",
    "tp2_price": "止盈二档价",
    "total_score": "总分",
    "breakout_readiness": "蓄势分",
    "accumulation_score": "吸筹分",
    "breakout_imminent_score": "突破前夜分",
    "imminent_vol_score": "缩量分",
    "imminent_amp_score": "振幅收敛分",
    "imminent_box_score": "箱体位置分",
    "imminent_ma_score": "均线粘合分",
    "imminent_wr_score": "WR趋势分(突破)",
    "imminent_mf_score": "资金天数分",
    "ma_cluster_tightness": "均线粘合度",
    "amp_contraction_pct": "振幅收敛分位",
    "volume_contraction_score": "量能收缩分",
    "box_position_60d": "箱体位置",
    "box_signal": "箱体信号",
    "entry_type": "入场类型",
    "wr_fast": "W&R快线",
    "wr_slow": "W&R慢线",
    "wr_down_days": "W&R走低天数",
    "wr_trend_score": "W&R趋势分",
    "mf_net_rate_sum": "主力净流入占比累计",
    "mf_pos_days": "主力净流入为正天数",
    "top_inst_net_buy_sum_wan": "机构净买入累计(万)",
    "valuation_score_5y": "5年估值分",
    "concept_rotation_score": "板块轮动分",
    "risk_hint": "风险提示",
    "is_trade_signal": "是否买入信号",
    "selection_tier": "名单类型",
    "build_plan": "建仓策略",
    "hold_plan": "持仓策略",
    "take_profit_plan": "止盈策略",
    "stop_loss_plan": "止损策略",
}


def load_theme_config(config_path: str = "config/themes.json") -> dict:
    """
    从统一 concepts.yaml 加载主题配置（回退旧 themes.json）。
    """
    from quant_data.concepts import load_concepts

    default_cfg = {
        "keywords": DEFAULT_THEME_KEYWORDS,
        "aliases": {},
        "fuzzy_cutoff": 0.55,
        "fuzzy_top_n": 2,
        "extra_ths_indices": [],
    }

    c = load_concepts()
    aliases = c.get_aliases()
    themes = c.get_themes() or DEFAULT_THEME_KEYWORDS
    keywords = [str(x).strip() for x in themes if str(x).strip()] or DEFAULT_THEME_KEYWORDS

    if not aliases:
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                kws = data.get("fifteenth_five_themes", [])
                if isinstance(kws, list):
                    keywords = [str(x).strip() for x in kws if str(x).strip()] or DEFAULT_THEME_KEYWORDS
                aliases_raw = data.get("aliases", {})
                if isinstance(aliases_raw, dict):
                    for k, v in aliases_raw.items():
                        if not isinstance(v, list):
                            continue
                        cleaned = [str(x).strip() for x in v if str(x).strip()]
                        if cleaned:
                            aliases[str(k).strip()] = cleaned
            except (json.JSONDecodeError, OSError):
                pass

    return {
        "keywords": keywords,
        "aliases": aliases,
        "fuzzy_cutoff": default_cfg["fuzzy_cutoff"],
        "fuzzy_top_n": default_cfg["fuzzy_top_n"],
        "extra_ths_indices": default_cfg["extra_ths_indices"],
    }


def load_risk_config(config_path: str = "config/risk.json") -> dict:
    if not os.path.exists(config_path):
        return DEFAULT_RISK_CONFIG.copy()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print("警告: risk.json 格式错误，已回退默认风险阈值。")
        return DEFAULT_RISK_CONFIG.copy()

    cfg = DEFAULT_RISK_CONFIG.copy()
    for key, default_value in DEFAULT_RISK_CONFIG.items():
        value = data.get(key, default_value)
        try:
            cfg[key] = float(value)
        except (TypeError, ValueError):
            cfg[key] = default_value
    if isinstance(data.get("v4_overrides"), dict):
        cfg["v4_overrides"] = {k: v for k, v in data["v4_overrides"].items() if not str(k).startswith("_")}
    return cfg


def load_valuation_config(config_path: str = "config/valuation.json") -> dict:
    cfg = DEFAULT_VALUATION_CONFIG.copy()
    if not os.path.exists(config_path):
        return cfg
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print("警告: valuation.json 格式错误，已回退默认估值配置。")
        return cfg
    for k, v in cfg.items():
        x = data.get(k, v)
        try:
            cfg[k] = float(x) if isinstance(v, float) else int(x)
        except (TypeError, ValueError):
            cfg[k] = v
    return cfg


def load_trade_config(config_path: str = "config/trade.json") -> dict:
    cfg = DEFAULT_TRADE_CONFIG.copy()
    if not os.path.exists(config_path):
        return cfg
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print("警告: trade.json 格式错误，已回退默认交易配置。")
        return cfg
    for k, v in cfg.items():
        x = data.get(k, v)
        try:
            cfg[k] = float(x) if isinstance(v, float) else int(x)
        except (TypeError, ValueError):
            cfg[k] = v
    for k, v in data.items():
        if k in cfg:
            continue
        if isinstance(v, dict):
            cfg[k] = {kk: vv for kk, vv in v.items() if not str(kk).startswith("_")}
        else:
            cfg[k] = v
    return cfg


def load_strategy_config(config_path: str = "config/strategy.json") -> dict:
    cfg = DEFAULT_STRATEGY_CONFIG.copy()
    if not os.path.exists(config_path):
        return cfg
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print("警告: strategy.json 格式错误，已回退默认策略模式。")
        return cfg
    mode = str(data.get("mode", cfg["mode"])).strip().lower()
    if mode not in ("v4_pure", "hybrid", "dual_engine"):
        mode = cfg["mode"]
    cfg["mode"] = mode
    return cfg


def load_cross_flow_config(config_path: str = "config/cross_flow.json") -> dict:
    env_p = os.environ.get("CROSS_FLOW_CONFIG", "").strip()
    if env_p and os.path.isabs(env_p) and os.path.exists(env_p):
        config_path = env_p
    elif env_p:
        rel = Path(env_p)
        if rel.exists():
            config_path = str(rel.resolve())
    cfg = DEFAULT_CROSS_FLOW_CONFIG.copy()
    if not os.path.exists(config_path):
        return cfg
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print("警告: cross_flow.json 格式错误，已回退默认金叉资金配置。")
        return cfg
    for k, v in cfg.items():
        x = data.get(k, v)
        try:
            cfg[k] = float(x) if isinstance(v, float) else int(x)
        except (TypeError, ValueError):
            cfg[k] = v
    return cfg


def apply_mode_overrides(cfg: dict, active_mode: str, override_key: Optional[str] = None) -> dict:
    if not isinstance(cfg, dict):
        return cfg
    out = dict(cfg)
    candidate_keys = []
    if override_key:
        candidate_keys.append(override_key)
    elif active_mode == "v4_pure":
        candidate_keys.append("v4_overrides")
    elif active_mode == "hybrid":
        candidate_keys.append("hybrid_overrides")
    for k in candidate_keys:
        ov = cfg.get(k)
        if isinstance(ov, dict):
            for kk, vv in ov.items():
                if str(kk).startswith("_"):
                    continue
                out[kk] = vv
    return out


def resolve_scoring_mode(strategy_mode: str, regime: str) -> str:
    if strategy_mode == "dual_engine":
        return "hybrid" if regime == "bull" else "v4_pure"
    return strategy_mode


def to_chinese_columns(df: pd.DataFrame) -> pd.DataFrame:
    df_new = df.copy()
    if "box_signal" in df_new.columns:
        box_sig_map = {
            "BREAKOUT_UP": "放量突破",
            "FAKE_BREAKOUT": "假突破预警",
            "PULLBACK_SUPPORT": "缩量回踩确认",
            "BREAKDOWN_DOWN": "向下跌破",
            "BUY_ZONE": "近下沿(买入区)",
            "SELL_ZONE": "近上沿(卖出区)",
            "HOLD": "箱体中部(观望)",
        }
        df_new["box_signal"] = df_new["box_signal"].map(lambda x: box_sig_map.get(str(x).strip(), str(x)))
    cols = {c: OUTPUT_COL_CN_MAP[c] for c in df_new.columns if c in OUTPUT_COL_CN_MAP}
    return df_new.rename(columns=cols)


class DataFetcher:
    """Tushare 数据获取封装（DataProvider 本地优先回退 Tushare）"""

    def __init__(self, token: str):
        from quant_data import get_provider, DAILY_BASIC_FIELDS

        self.pro = get_provider(token=token)

    @staticmethod
    def _to_ts_date(date_str: str) -> str:
        return date_str.replace("-", "")

    @staticmethod
    def is_mainboard(ts_code: str) -> bool:
        """包含沪深主板、创业板(300/301)、科创板(688)，仅排除北交所"""
        if ts_code.endswith((".SH", ".SZ")):
            return True
        return False

    def get_index_daily(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        df = self.pro.index_daily(
            ts_code=index_code,
            start_date=self._to_ts_date(start_date),
            end_date=self._to_ts_date(end_date),
        )
        if df.empty:
            raise ValueError(f"指数无数据: {index_code}")
        df = df.sort_values("trade_date")
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date")
        df["volume"] = df["vol"]
        return df[["open", "high", "low", "close", "volume", "pct_chg"]]

    def get_latest_trade_date(self, end_date: str) -> str:
        ts_end = self._to_ts_date(end_date)
        start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=20)).strftime("%Y-%m-%d")
        ts_start = self._to_ts_date(start)
        cal = self.pro.trade_cal(exchange="SSE", start_date=ts_start, end_date=ts_end)
        if cal.empty:
            return end_date
        opens = cal[cal["is_open"] == 1].sort_values("cal_date")
        if opens.empty:
            return end_date
        latest = opens.iloc[-1]["cal_date"]
        return datetime.strptime(latest, "%Y%m%d").strftime("%Y-%m-%d")

    def get_stock_basic_mainboard(self) -> pd.DataFrame:
        basic = self.pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,area,industry,market")
        basic = basic[basic["ts_code"].map(self.is_mainboard)].copy()
        basic = basic[~basic["name"].str.contains("ST", na=False)]
        return basic

    def _ths_index_theme_pool(
        self,
        keywords: list[str],
        alias_map: Optional[dict[str, list[str]]] = None,
    ) -> pd.DataFrame:
        alias_map = alias_map or {}
        try:
            idx = self.pro.ths_index()
        except Exception:
            return pd.DataFrame(columns=["ts_code", "concept_name"])
        if idx is None or idx.empty:
            return pd.DataFrame(columns=["ts_code", "concept_name"])

        idx = idx.copy()
        idx["name"] = idx["name"].astype(str)

        index_hits: dict[str, str] = {}

        def register_term(term: str) -> None:
            term = str(term).strip()
            if len(term) < 2:
                return
            eq = idx[idx["name"] == term]
            for _, row in eq.iterrows():
                index_hits[str(row["ts_code"])] = str(row["name"])
            if len(term) >= 3:
                pat = re.escape(term)
                sub = idx[idx["name"].str.contains(pat, case=False, na=False, regex=True)]
                for _, row in sub.iterrows():
                    index_hits[str(row["ts_code"])] = str(row["name"])

        for kw in keywords:
            kw = str(kw).strip()
            if not kw:
                continue
            register_term(kw)
            for a in alias_map.get(kw, []):
                register_term(str(a))

        parts: list[pd.DataFrame] = []
        for index_ts_code, display_name in index_hits.items():
            try:
                m = self.pro.ths_member(ts_code=index_ts_code)
            except Exception:
                continue
            if m is None or m.empty or "con_code" not in m.columns:
                continue
            chunk = m[["con_code"]].rename(columns={"con_code": "ts_code"})
            chunk["concept_name"] = display_name
            parts.append(chunk)

        if not parts:
            return pd.DataFrame(columns=["ts_code", "concept_name"])
        out = pd.concat(parts, ignore_index=True)
        return out.drop_duplicates(subset=["ts_code", "concept_name"])

    def _ths_explicit_indices_members(self, entries: Optional[list[dict[str, str]]]) -> pd.DataFrame:
        if not entries:
            return pd.DataFrame(columns=["ts_code", "concept_name"])
        parts: list[pd.DataFrame] = []
        for raw in entries:
            idx_code = str(raw.get("ts_code", "")).strip()
            if not idx_code:
                continue
            label = str(raw.get("label", idx_code)).strip() or idx_code
            try:
                m = self.pro.ths_member(ts_code=idx_code)
            except Exception:
                continue
            if m is None or m.empty or "con_code" not in m.columns:
                continue
            chunk = m[["con_code"]].rename(columns={"con_code": "ts_code"})
            chunk["concept_name"] = label
            parts.append(chunk)
        if not parts:
            return pd.DataFrame(columns=["ts_code", "concept_name"])
        out = pd.concat(parts, ignore_index=True)
        return out.drop_duplicates(subset=["ts_code", "concept_name"])

    def get_theme_pool(
        self,
        keywords: list[str],
        alias_map: Optional[dict[str, list[str]]] = None,
        fuzzy_cutoff: float = 0.55,
        fuzzy_top_n: int = 2,
        extra_ths_indices: Optional[list[dict[str, str]]] = None,
    ) -> pd.DataFrame:
        concept = self.pro.concept(src="ts")
        if concept is None:
            concept = pd.DataFrame(columns=["code", "name"])

        alias_map = alias_map or {}

        if concept.empty:
            concept_names: list[str] = []
            name2code = {}
        else:
            concept_names = concept["name"].astype(str).tolist()
            name2code = dict(zip(concept["name"], concept["code"]))
        selected: dict[str, str] = {}
        match_records = []

        for kw in keywords:
            kw = str(kw).strip()
            if not kw:
                continue

            direct = concept[concept["name"].str.contains(kw, case=False, na=False)]
            picked_names = direct["name"].tolist()
            method = "direct"

            if not picked_names:
                aliases = alias_map.get(kw, [kw])
                alias_hit = concept[concept["name"].str.contains("|".join(aliases), case=False, na=False)]
                picked_names = alias_hit["name"].tolist()
                method = "alias"

            if not picked_names and concept_names:
                fuzzy = difflib.get_close_matches(kw, concept_names, n=int(fuzzy_top_n), cutoff=float(fuzzy_cutoff))
                picked_names = fuzzy
                method = "fuzzy"

            for nm in picked_names:
                code = name2code.get(nm)
                if code:
                    selected[code] = nm
            match_records.append({"keyword": kw, "method": method, "matched_concepts": len(picked_names)})

        ths_df = self._ths_index_theme_pool(keywords, alias_map)

        if match_records:
            report = pd.DataFrame(match_records)
            unresolved = report[report["matched_concepts"] == 0]["keyword"].tolist()
            print(f"主题匹配统计: 共{len(report)}个关键词，0命中={len(unresolved)}")
            if unresolved:
                print("未命中关键词:", ",".join(unresolved))

        if not ths_df.empty:
            uniq_boards = ths_df["concept_name"].nunique()
            print(f"同花顺指数(ths_index)补充: {uniq_boards} 个板块, {len(ths_df)} 条成分(去重前)")

        explicit_df = self._ths_explicit_indices_members(extra_ths_indices or [])
        if not explicit_df.empty:
            print(
                f"显式同花顺指数(extra_ths_indices): "
                f"{explicit_df['concept_name'].nunique()} 个板块, {len(explicit_df)} 条成分(去重前)"
            )

        members = []
        for code, name in selected.items():
            detail = self.pro.concept_detail(id=code, fields="id,concept_name,ts_code,name")
            if detail.empty:
                continue
            detail["concept_name"] = name
            members.append(detail[["ts_code", "concept_name"]])
        frames: list[pd.DataFrame] = list(members)
        if not ths_df.empty:
            frames.append(ths_df)
        if not explicit_df.empty:
            frames.append(explicit_df)
        if not frames:
            return pd.DataFrame(columns=["ts_code", "concept_name"])
        merged = pd.concat(frames, ignore_index=True)
        return merged.drop_duplicates(subset=["ts_code"], keep="last")

    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        df = self.pro.daily(ts_code=ts_code, start_date=self._to_ts_date(start_date), end_date=self._to_ts_date(end_date))
        if df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date")
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date")
        df["volume"] = df["vol"]
        df["pct_change"] = df["pct_chg"]
        df["amount"] = pd.to_numeric(df.get("amount", np.nan), errors="coerce")
        df["date"] = df.index
        return df[["open", "high", "low", "close", "volume", "amount", "pct_change", "vol", "date"]]

    def get_daily_basic(self, ts_codes: list[str], trade_date: str) -> pd.DataFrame:
        ts_trade_date = self._to_ts_date(trade_date)
        df = self.pro.daily_basic(trade_date=ts_trade_date, fields=DAILY_BASIC_FIELDS)
        if df.empty:
            return pd.DataFrame(columns=["ts_code", "total_mv", "circ_mv", "pb", "pe_ttm", "turnover_rate"])
        return df[df["ts_code"].isin(ts_codes)].drop_duplicates(subset=["ts_code"])

    def get_daily_basic_history(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        cache_dir = Path(".cache/valuation")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{ts_code}.csv"

        def _fetch(s: str, e: str) -> pd.DataFrame:
            return self.pro.daily_basic(
                ts_code=ts_code,
                start_date=self._to_ts_date(s),
                end_date=self._to_ts_date(e),
                fields="trade_date,ts_code,pe_ttm,pb",
            )

        if cache_file.exists():
            hist = pd.read_csv(cache_file, dtype={"trade_date": str})
            if hist.empty:
                return hist
            max_cached = hist["trade_date"].max()
            need_refresh = max_cached < self._to_ts_date(end_date)
            if need_refresh:
                next_day = (datetime.strptime(max_cached, "%Y%m%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                inc = _fetch(next_day, end_date)
                if not inc.empty:
                    for _req_col in ("pe_ttm", "pb"):
                        if _req_col not in inc.columns:
                            inc[_req_col] = float("nan")
                    hist = pd.concat([hist, inc], ignore_index=True).drop_duplicates(subset=["trade_date"])
                    hist = hist.sort_values("trade_date")
                    hist.to_csv(cache_file, index=False, encoding="utf-8-sig")
        else:
            hist = _fetch(start_date, end_date)
            if hist.empty:
                return hist
            for _req_col in ("pe_ttm", "pb"):
                if _req_col not in hist.columns:
                    hist[_req_col] = float("nan")
            hist = hist.sort_values("trade_date")
            hist.to_csv(cache_file, index=False, encoding="utf-8-sig")

        s = self._to_ts_date(start_date)
        e = self._to_ts_date(end_date)
        return hist[(hist["trade_date"] >= s) & (hist["trade_date"] <= e)].copy()

    def get_moneyflow_dc(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        try:
            df = self.pro.moneyflow_dc(ts_code=ts_code, start_date=self._to_ts_date(start_date), end_date=self._to_ts_date(end_date))
        except Exception:
            return pd.DataFrame()
        if df.empty:
            return df
        if "trade_date" in df.columns:
            df = df.sort_values("trade_date")
        return df

    def get_top_inst_by_dates(self, trade_dates: list[str]) -> pd.DataFrame:
        out = []
        for d in trade_dates:
            d8 = str(d).replace("-", "")
            try:
                df = self.pro.top_inst(trade_date=d8)
            except Exception:
                df = pd.DataFrame()
            if df is None or df.empty:
                continue
            if "trade_date" not in df.columns:
                df["trade_date"] = d8
            out.append(df)
        if not out:
            return pd.DataFrame()
        return pd.concat(out, ignore_index=True)


class MarketRegimeDetector:
    def __init__(self, index_map: dict[str, pd.DataFrame]):
        self.index_map = index_map

    @staticmethod
    def _single_index_regime(df: pd.DataFrame) -> tuple[str, int]:
        d = df.copy()
        close = d["close"]
        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ret20 = close.iloc[-1] / close.iloc[-21] - 1 if len(close) >= 21 else np.nan
        up_ratio = (d["pct_chg"].tail(21) > 0).mean() if "pct_chg" in d.columns else 0.5

        score = 0
        score += 1 if close.iloc[-1] > ma20 else -1
        score += 1 if ma5 > ma20 else -1
        score += 1 if pd.notna(ret20) and ret20 > 0 else -1
        score += 1 if up_ratio >= 0.5 else -1

        if score >= 3:
            return "bull", score
        if score <= -3:
            return "bear", score
        return "range", score

    def regime(self) -> str:
        votes = {"bull": 0, "range": 0, "bear": 0}
        scores = []
        for _, df in self.index_map.items():
            if df is None or df.empty or len(df) < 30:
                continue
            r, s = self._single_index_regime(df)
            votes[r] += 1
            scores.append(s)
        if sum(votes.values()) == 0:
            return "range"
        if votes["bull"] >= 2:
            return "bull"
        if votes["bear"] >= 2:
            return "bear"
        score_sum = sum(scores)
        if score_sum > 0:
            return "bull"
        if score_sum < 0:
            return "bear"
        return "range"


class FactorCalculator:
    def __init__(self, stock_dfs: dict[str, pd.DataFrame], daily_basic_df: pd.DataFrame):
        self.stock_dfs = stock_dfs
        self.daily_basic = daily_basic_df.set_index("ts_code") if not daily_basic_df.empty else pd.DataFrame()

    def momentum_20d(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 25:
            return np.nan
        return float(df["close"].iloc[-1] / df["close"].iloc[-21] - 1)

    def breakout_readiness(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 40:
            return np.nan
        close = df["close"]
        vol = df["volume"]
        ma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        upper = ma20 + 2 * std20
        band_width = (upper - (ma20 - 2 * std20)) / ma20
        width_score = 1 - np.clip(float(band_width.iloc[-1]), 0, 0.25) / 0.25
        resistance_20 = close.rolling(20).max().iloc[-2]
        near_break = np.clip((close.iloc[-1] / resistance_20 - 0.97) / 0.03, 0, 1)
        vol_ratio = vol.rolling(5).mean().iloc[-1] / max(vol.rolling(20).mean().iloc[-1], 1)
        vol_score = np.clip((vol_ratio - 0.8) / 0.7, 0, 1)
        return 100 * (0.4 * width_score + 0.4 * near_break + 0.2 * vol_score)

    def ma_structure(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 70:
            return np.nan
        c = df["close"]
        ma5 = c.rolling(5).mean().iloc[-1]
        ma10 = c.rolling(10).mean().iloc[-1]
        ma20 = c.rolling(20).mean().iloc[-1]
        ma60 = c.rolling(60).mean().iloc[-1]
        s = 0
        if ma5 > ma10:
            s += 30
        if ma10 > ma20:
            s += 30
        if ma20 > ma60:
            s += 40
        return float(s)

    def volatility_30d(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 40:
            return np.nan
        r = df["close"].pct_change().dropna().tail(30)
        return float(r.std() * np.sqrt(252))

    def turnover_rate(self, code: str) -> float:
        if self.daily_basic.empty or code not in self.daily_basic.index:
            return np.nan
        return float(self.daily_basic.loc[code].get("turnover_rate", np.nan))

    def pb(self, code: str) -> float:
        if self.daily_basic.empty or code not in self.daily_basic.index:
            return np.nan
        return float(self.daily_basic.loc[code].get("pb", np.nan))

    def pe_ttm(self, code: str) -> float:
        if self.daily_basic.empty or code not in self.daily_basic.index:
            return np.nan
        return float(self.daily_basic.loc[code].get("pe_ttm", np.nan))

    def runup_10d(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 12:
            return np.nan
        return float(df["close"].iloc[-1] / df["close"].iloc[-11] - 1)

    def near_high_20d_ratio(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 25:
            return np.nan
        h20 = float(df["high"].rolling(20).max().iloc[-1])
        c = float(df["close"].iloc[-1])
        if h20 <= 0:
            return np.nan
        return c / h20

    def macd_hist_slope(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 40:
            return np.nan
        close = df["close"]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        hist = macd - signal
        y = hist.tail(5).values
        x = np.arange(len(y))
        if len(y) < 5:
            return np.nan
        slope = np.polyfit(x, y, 1)[0]
        return float(slope)

    def macd_hist_expand_days(self, code: str, lookback_days: int = 3) -> int:
        df = self.stock_dfs.get(code, pd.DataFrame())
        lb = int(lookback_days)
        if len(df) < 40 or lb < 2:
            return 0
        close = df["close"]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        hist = macd - signal
        s = hist.tail(lb).dropna()
        if len(s) < 2:
            return 0
        return int((s.diff() > 0).sum())

    def obv_slope_20d(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 40:
            return np.nan
        close = df["close"]
        vol = df["volume"]
        direction = np.sign(close.diff().fillna(0))
        obv = (direction * vol).cumsum()
        y = obv.tail(20).values
        x = np.arange(len(y))
        if len(y) < 20:
            return np.nan
        slope = np.polyfit(x, y, 1)[0]
        base = max(abs(obv.tail(20).mean()), 1.0)
        return float(slope / base * 100)

    def volume_contraction_score(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 25:
            return np.nan
        vol = df["volume"]
        vol5 = float(vol.tail(5).mean())
        vol20 = float(vol.tail(20).mean())
        if vol20 <= 0:
            return np.nan
        ratio = vol5 / vol20
        if ratio <= 0.5 or ratio >= 1.6:
            return 0.0
        if 0.7 <= ratio <= 1.0:
            return 100.0
        if 0.5 < ratio < 0.7:
            return float(np.clip((ratio - 0.5) / (0.7 - 0.5) * 100.0, 0.0, 100.0))
        return float(np.clip((1.6 - ratio) / (1.6 - 1.0) * 100.0, 0.0, 100.0))

    def amplitude_contraction_pct(self, code: str, window: int = 60) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < window + 5:
            return np.nan
        amp = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)
        amp = amp.dropna()
        if len(amp) < window + 5:
            return np.nan
        recent5 = float(amp.tail(5).mean())
        hist_window = amp.tail(window + 5).head(window)
        if hist_window.empty:
            return np.nan
        pct = float((hist_window <= recent5).mean())
        return pct

    def ma_cluster_tightness(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 25:
            return np.nan
        c = df["close"]
        ma5 = float(c.rolling(5).mean().iloc[-1])
        ma10 = float(c.rolling(10).mean().iloc[-1])
        ma20 = float(c.rolling(20).mean().iloc[-1])
        if ma20 <= 0 or any(pd.isna(x) for x in (ma5, ma10, ma20)):
            return np.nan
        spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20
        score = float(np.clip((0.03 - spread) / 0.03 * 100.0, 0.0, 100.0))
        return score

    def box_position_60d(self, code: str) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 65:
            return np.nan
        h = float(df["high"].shift(1).rolling(60).max().iloc[-1])
        l = float(df["low"].shift(1).rolling(60).min().iloc[-1])
        c = float(df["close"].iloc[-1])
        if pd.isna(h) or pd.isna(l) or h <= l:
            return np.nan
        return (c - l) / (h - l)

    def williams_r(self, code: str, period: int = 14) -> float:
        df = self.stock_dfs.get(code, pd.DataFrame())
        n = int(period)
        if n < 2 or len(df) < n + 2:
            return np.nan
        hh = float(df["high"].tail(n).max())
        ll = float(df["low"].tail(n).min())
        c = float(df["close"].iloc[-1])
        if hh <= ll:
            return np.nan
        return float((hh - c) / (hh - ll) * 100)

    def williams_r_down_days(self, code: str, period: int = 14, lookback_days: int = 3) -> int:
        df = self.stock_dfs.get(code, pd.DataFrame())
        n = int(period)
        lb = int(lookback_days)
        if n < 2 or lb < 1 or len(df) < n + lb + 2:
            return 0
        wr_vals = []
        for i in range(lb + 1, 0, -1):
            sub = df.iloc[: len(df) - i + 1]
            hh = float(sub["high"].tail(n).max())
            ll = float(sub["low"].tail(n).min())
            c = float(sub["close"].iloc[-1])
            if hh <= ll:
                wr_vals.append(np.nan)
            else:
                wr_vals.append((hh - c) / (hh - ll) * 100)
        s = pd.Series(wr_vals).dropna()
        if len(s) < 2:
            return 0
        return int((s.diff() < 0).sum())

    def williams_r_trend_score(
        self,
        code: str,
        fast_period: int = 14,
        slow_period: int = 28,
        lookback_days: int = 3,
    ) -> float:
        wr_fast = self.williams_r(code, fast_period)
        wr_slow = self.williams_r(code, slow_period)
        down_days = self.williams_r_down_days(code, period=fast_period, lookback_days=lookback_days)
        if pd.isna(wr_fast):
            return np.nan
        level_score = np.clip((100 - wr_fast) / 80 * 100, 0, 100)
        rel_score = 50.0
        if pd.notna(wr_slow):
            diff = wr_slow - wr_fast
            rel_score = np.clip(50 + diff * 1.5, 0, 100)
        max_days = max(int(lookback_days), 1)
        down_score = np.clip(down_days / max_days * 100, 0, 100)
        return float(level_score * 0.40 + rel_score * 0.30 + down_score * 0.30)

    def cross_signal_features(
        self,
        code: str,
        lookback_days: int = 3,
        near_min_ratio: float = 1.15,
        near_min_window: int = 8,
        is_pre_cross_shrink_ratio_max: float = 0.9,
    ) -> dict:
        df = self.stock_dfs.get(code, pd.DataFrame())
        lb = int(lookback_days)
        if len(df) < 30 or lb < 2:
            return {
                "ma5_ma10_gap_now": np.nan,
                "ma5_ma10_gap_shrink_ratio": np.nan,
                "is_pre_cross": False,
                "is_just_cross": False,
            }
        close = df["close"]
        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        gap = (ma10 - ma5).dropna()
        if len(gap) < lb + 2:
            return {
                "ma5_ma10_gap_now": np.nan,
                "ma5_ma10_gap_shrink_ratio": np.nan,
                "ma5_ma10_gap_pct_now": np.nan,
                "ma5_ma10_gap_near_min": False,
                "is_pre_cross": False,
                "is_just_cross": False,
                "cross_age_days": np.nan,
            }
        now_gap = float(gap.iloc[-1])
        prev_gap = float(gap.iloc[-(lb + 1)])
        ma10_now = float(ma10.iloc[-1]) if pd.notna(ma10.iloc[-1]) else np.nan
        gap_pct_now = now_gap / ma10_now if pd.notna(ma10_now) and ma10_now > 0 else np.nan
        pos_gap = gap[gap > 0]
        if len(pos_gap) >= 2:
            near_window = max(lb + 2, int(near_min_window))
            gap_min_recent = float(pos_gap.tail(near_window).min())
            gap_near_min = bool(pd.notna(gap_min_recent) and gap_min_recent > 0 and now_gap <= gap_min_recent * float(near_min_ratio))
        else:
            gap_near_min = False
        shrink_ratio = np.nan
        if prev_gap > 0:
            shrink_ratio = now_gap / prev_gap
        is_pre = bool(now_gap > 0 and pd.notna(shrink_ratio) and shrink_ratio < float(is_pre_cross_shrink_ratio_max))
        is_cross_today = bool(ma5.iloc[-1] >= ma10.iloc[-1] and ma5.iloc[-2] < ma10.iloc[-2])
        last_cross_offset = np.nan
        max_scan = min(10, len(df) - 1)
        for k in range(1, max_scan + 1):
            i = len(ma5) - k
            if i - 1 < 0:
                break
            if pd.notna(ma5.iloc[i]) and pd.notna(ma10.iloc[i]) and pd.notna(ma5.iloc[i - 1]) and pd.notna(ma10.iloc[i - 1]):
                if ma5.iloc[i] >= ma10.iloc[i] and ma5.iloc[i - 1] < ma10.iloc[i - 1]:
                    last_cross_offset = k - 1
                    break
        return {
            "ma5_ma10_gap_now": now_gap,
            "ma5_ma10_gap_shrink_ratio": shrink_ratio,
            "ma5_ma10_gap_pct_now": gap_pct_now,
            "ma5_ma10_gap_near_min": gap_near_min,
            "is_pre_cross": is_pre,
            "is_just_cross": is_cross_today,
            "cross_age_days": last_cross_offset,
        }


class StockScoringEngine:
    def __init__(self, calc: FactorCalculator, regime: str, meta_df: Optional[pd.DataFrame] = None):
        self.calc = calc
        self.regime = regime
        self.meta_df = meta_df.copy() if meta_df is not None else pd.DataFrame(columns=["ts_code", "industry"])

    def factor_config(self):
        if self.regime == "bull":
            return [
                ("breakout_readiness", 0.20, self.calc.breakout_readiness, "positive"),
                ("momentum_20d", 0.30, self.calc.momentum_20d, "positive"),
                ("ma_structure", 0.35, self.calc.ma_structure, "positive"),
                ("volatility_30d", 0.15, self.calc.volatility_30d, "negative"),
            ]
        if self.regime == "bear":
            return [
                ("breakout_readiness", 0.15, self.calc.breakout_readiness, "positive"),
                ("volatility_30d", 0.30, self.calc.volatility_30d, "negative"),
                ("pb", 0.20, self.calc.pb, "negative"),
                ("turnover_rate", 0.35, self.calc.turnover_rate, "negative"),
            ]
        return [
            ("breakout_readiness", 0.20, self.calc.breakout_readiness, "positive"),
            ("ma_structure", 0.35, self.calc.ma_structure, "positive"),
            ("momentum_20d", 0.20, self.calc.momentum_20d, "positive"),
            ("volatility_30d", 0.10, self.calc.volatility_30d, "negative"),
            ("pe_ttm", 0.15, self.calc.pe_ttm, "negative"),
        ]

    @staticmethod
    def normalize_cross_section(series: pd.Series) -> pd.Series:
        s = series.copy()
        x = s.dropna()
        if x.empty:
            return pd.Series(50.0, index=s.index)
        low = x.quantile(0.01)
        high = x.quantile(0.99)
        x = x.clip(low, high)
        std = x.std()
        if std == 0 or pd.isna(std):
            return pd.Series(50.0, index=s.index)
        z = (x - x.mean()) / std
        score = (50 + 12 * z).clip(0, 100)
        out = pd.Series(50.0, index=s.index)
        out.loc[score.index] = score
        return out

    def rank(self, stock_codes: list[str]) -> pd.DataFrame:
        conf = self.factor_config()
        raw = pd.DataFrame(index=stock_codes)
        for name, _, func, _ in conf:
            raw[name] = [func(code) for code in stock_codes]

        score_df = pd.DataFrame(index=stock_codes)
        for name, _, _, direction in conf:
            sc = self.normalize_cross_section(raw[name])
            if direction == "negative":
                sc = 100 - sc
            score_df[name] = sc

        total = pd.Series(0.0, index=stock_codes)
        for name, w, _, _ in conf:
            total += score_df[name] * w

        out = pd.DataFrame(
            {
                "ts_code": stock_codes,
                "regime": self.regime,
                "total_score": total.values,
                "breakout_readiness": raw.get("breakout_readiness", pd.Series(index=stock_codes)).values,
            }
        )
        if not self.meta_df.empty:
            merged = out.merge(self.meta_df[["ts_code", "industry"]], on="ts_code", how="left")
            merged = merged.merge(
                pd.DataFrame({"ts_code": stock_codes, "runup_10d": [self.calc.runup_10d(c) for c in stock_codes]}),
                on="ts_code",
                how="left",
            )
            runup_penalty = self.normalize_cross_section(merged["runup_10d"]).fillna(50)
            merged["total_score"] = merged["total_score"] * 0.85 + (100 - runup_penalty) * 0.15
            out = merged
        return out.sort_values("total_score", ascending=False).reset_index(drop=True)


def build_candidate_pool(
    fetcher: DataFetcher,
    theme_keywords: list[str],
    alias_map: Optional[dict[str, list[str]]] = None,
    fuzzy_cutoff: float = 0.55,
    fuzzy_top_n: int = 2,
    extra_ths_indices: Optional[list[dict[str, str]]] = None,
) -> pd.DataFrame:
    basic = fetcher.get_stock_basic_mainboard()
    theme_pool = fetcher.get_theme_pool(
        theme_keywords,
        alias_map=alias_map,
        fuzzy_cutoff=fuzzy_cutoff,
        fuzzy_top_n=fuzzy_top_n,
        extra_ths_indices=extra_ths_indices,
    )
    if theme_pool.empty:
        raise ValueError("未获取到十五五主题池，请检查关键词或接口权限。")
    pool = basic.merge(theme_pool, on="ts_code", how="inner")
    pool = pool.drop_duplicates(subset=["ts_code"])
    return pool


__all__ = [
    # defaults
    "DEFAULT_THEME_KEYWORDS",
    "DEFAULT_RISK_CONFIG",
    "DEFAULT_VALUATION_CONFIG",
    "DEFAULT_STRATEGY_CONFIG",
    "DEFAULT_TRADE_CONFIG",
    "DEFAULT_CROSS_FLOW_CONFIG",
    # IO helpers
    "OUTPUT_COL_CN_MAP",
    "to_chinese_columns",
    # config loaders / mode helpers
    "load_theme_config",
    "load_risk_config",
    "load_valuation_config",
    "load_trade_config",
    "load_strategy_config",
    "load_cross_flow_config",
    "apply_mode_overrides",
    "resolve_scoring_mode",
]
