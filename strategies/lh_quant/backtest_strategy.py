"""
策略历史回测（5-10年）

目标：
1) 回测当前选股逻辑在历史区间的胜率和收益表现
2) 降低接口压力：本地缓存 + 请求节流 + 低频（月度）调仓

说明：
- 当前主题池使用“当前概念映射”回溯历史，存在一定幸存者偏差。
- 该版本主要用于策略方向验证，不作为实盘业绩承诺。
"""

import argparse
import json
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from execution_policy import dynamic_decay_exit_signal, macd_hist_slope_recent
from searchv1 import (
    DataFetcher,
    MarketRegimeDetector,
    apply_mode_overrides,
    build_candidate_pool,
    load_cross_flow_config,
    load_risk_config,
    load_theme_config,
    load_theme_keywords,
    load_trade_config,
    load_strategy_config,
    resolve_scoring_mode,
)


@dataclass
class ThrottleConfig:
    per_request_sleep: float = 0.12
    burst_requests: int = 120
    burst_sleep: float = 10.0


@dataclass
class RiskControlConfig:
    # 风险门槛（先过滤，再打分选股）
    max_volatility_30d: float = 0.50
    min_breakout_readiness: float = 55.0
    min_turnover_rate: float = 0.8
    max_turnover_rate: float = 10.0
    # 组合约束
    max_industry_ratio: float = 0.35  # 单行业最多占持仓比例
    # 持仓风控
    stop_loss_pct: float = 0.08
    # v0.5 新加: 与 searchv1 risk.json 对齐, 用于 v4_pure(盘整启动专用) 收紧过滤
    max_runup_10d_pct: float = 12.0
    max_near_high_20d_ratio: float = 0.97
    min_accumulation_score: float = 58.0
    box_low_position_min: float = 0.35
    box_low_position_max: float = 0.75


@dataclass
class TradeExecutionConfig:
    max_daily_picks: int = 5
    entry_breakout_buffer_pct: float = 0.2
    entry_need_above_ma5: float = 1.0
    entry_min_ma5_slope_5d: float = 0.0
    hard_stop_loss_pct: float = 0.06
    tp1_pct: float = 0.08
    tp2_pct: float = 0.15
    context_exit_require_both: int = 1
    context_exit_only_when_profit: int = 1
    context_exit_min_hold_days: int = 2
    context_exit_confirm_days: int = 2
    context_exit_sector_lookback_days: int = 3
    context_exit_sector_ret_mean_max: float = -0.008
    context_exit_sector_negative_ratio_min: float = 0.6
    context_exit_leader_count_min: int = 2
    context_exit_leader_weak_ratio_min: float = 0.5
    context_exit_leader_macd_slope_max: float = 0.0
    context_exit_auto_leader_n: int = 3
    context_exit_min_profit_to_arm: float = 0.03
    context_exit_trail_drawdown_pct: float = 0.025
    entry_rsi_death_veto_enabled: int = 1
    entry_rsi_death_line: float = 60.0
    entry_rsi_period: int = 14
    entry_rsi_death_veto_skip_pre_cross: int = 1
    floating_stop_loss_pct: float = 0.06
    floating_take_profit_pct: float = 0.12
    floating_min_profit_to_trail: float = 0.04
    floating_trail_drawdown_pct: float = 0.03
    context_leaders_by_concept: Dict[str, List[str]] = None
    # v0.6 P1: ATR 动态止损 + 收盘验证 + 紧急下限. 默认 0 = 沿用旧固定百分比逻辑
    atr_stop_enabled: int = 0
    atr_stop_period: int = 20
    atr_stop_multiplier: float = 1.5
    atr_stop_floor_pct: float = 0.03
    atr_stop_ceil_pct: float = 0.08
    stop_close_verify: int = 0
    emergency_stop_pct: float = 0.10


def load_backtest_risk_config(config_path: str = "config/backtest_risk.json") -> RiskControlConfig:
    """
    回测专用风控参数（顶层简化版）。v4_pure 的严格阈值见 load_searchv1_risk_dict。
    若 backtest_risk.json 不存在, 则从 config/risk.json 同步读取相关字段(v0.5+对齐)。
    """
    base = RiskControlConfig()
    kwargs: Dict[str, float] = {
        "max_volatility_30d": base.max_volatility_30d,
        "min_breakout_readiness": base.min_breakout_readiness,
        "min_turnover_rate": base.min_turnover_rate,
        "max_turnover_rate": base.max_turnover_rate,
        "max_industry_ratio": base.max_industry_ratio,
        "stop_loss_pct": base.stop_loss_pct,
        "max_runup_10d_pct": base.max_runup_10d_pct,
        "max_near_high_20d_ratio": base.max_near_high_20d_ratio,
        "min_accumulation_score": base.min_accumulation_score,
        "box_low_position_min": base.box_low_position_min,
        "box_low_position_max": base.box_low_position_max,
    }

    if os.path.exists(config_path):
        try:
            data = json.loads(Path(config_path).read_text(encoding="utf-8"))
            for k in list(kwargs.keys()):
                if k in data:
                    kwargs[k] = float(data[k])
        except (json.JSONDecodeError, OSError):
            print("警告: backtest_risk.json 格式错误，已回退默认回测风险参数。")

    risk_dict = load_searchv1_risk_dict()
    for k in ("max_runup_10d_pct", "max_near_high_20d_ratio", "min_accumulation_score",
              "box_low_position_min", "box_low_position_max"):
        if k in risk_dict:
            try:
                kwargs[k] = float(risk_dict[k])
            except (TypeError, ValueError):
                pass

    return RiskControlConfig(**kwargs)


def load_searchv1_risk_dict(config_path: str = "config/risk.json") -> dict:
    """加载 searchv1 共用的 risk.json (含 v4_overrides), 用于按 active_mode 切阈值。"""
    try:
        return load_risk_config(config_path)
    except Exception:
        if not os.path.exists(config_path):
            return {}
        try:
            return json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}


def load_searchv1_trade_dict(config_path: str = "config/trade.json") -> dict:
    """加载 searchv1 共用的 trade.json (含 v4_overrides)。"""
    try:
        return load_trade_config(config_path)
    except Exception:
        if not os.path.exists(config_path):
            return {}
        try:
            return json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}


def effective_risk_cfg(risk_cfg: RiskControlConfig, active_mode: str, raw_risk_dict: dict) -> RiskControlConfig:
    """v4_pure 时把 risk.json.v4_overrides 合并到 risk_cfg, 返回新实例。"""
    if active_mode != "v4_pure" or not raw_risk_dict:
        return risk_cfg
    merged = apply_mode_overrides(raw_risk_dict, active_mode)
    out = replace(risk_cfg)
    for k in (
        "max_runup_10d_pct",
        "max_near_high_20d_ratio",
        "min_accumulation_score",
        "box_low_position_min",
        "box_low_position_max",
    ):
        if k in merged:
            try:
                setattr(out, k, float(merged[k]))
            except (TypeError, ValueError):
                pass
    return out


def load_trade_execution_config(config_path: str = "config/trade.json") -> TradeExecutionConfig:
    cfg = load_trade_config(config_path)
    leaders_cfg = cfg.get("context_leaders_by_concept", {})
    if not isinstance(leaders_cfg, dict):
        leaders_cfg = {}
    leaders_map: Dict[str, List[str]] = {}
    for concept_name, raw_codes in leaders_cfg.items():
        if not isinstance(raw_codes, list):
            continue
        codes = [str(c).strip() for c in raw_codes if str(c).strip()]
        if codes:
            leaders_map[str(concept_name)] = codes
    return TradeExecutionConfig(
        max_daily_picks=int(cfg.get("max_daily_picks", 5)),
        entry_breakout_buffer_pct=float(cfg.get("entry_breakout_buffer_pct", 0.2)),
        entry_need_above_ma5=float(cfg.get("entry_need_above_ma5", 1.0)),
        entry_min_ma5_slope_5d=float(cfg.get("entry_min_ma5_slope_5d", 0.0)),
        hard_stop_loss_pct=float(cfg.get("hard_stop_loss_pct", 0.06)),
        tp1_pct=float(cfg.get("tp1_pct", 0.08)),
        tp2_pct=float(cfg.get("tp2_pct", 0.15)),
        context_exit_require_both=int(cfg.get("context_exit_require_both", 1)),
        context_exit_only_when_profit=int(cfg.get("context_exit_only_when_profit", 1)),
        context_exit_min_hold_days=int(cfg.get("context_exit_min_hold_days", 2)),
        context_exit_confirm_days=int(cfg.get("context_exit_confirm_days", 2)),
        context_exit_sector_lookback_days=int(cfg.get("context_exit_sector_lookback_days", 3)),
        context_exit_sector_ret_mean_max=float(cfg.get("context_exit_sector_ret_mean_max", -0.008)),
        context_exit_sector_negative_ratio_min=float(cfg.get("context_exit_sector_negative_ratio_min", 0.6)),
        context_exit_leader_count_min=int(cfg.get("context_exit_leader_count_min", 2)),
        context_exit_leader_weak_ratio_min=float(cfg.get("context_exit_leader_weak_ratio_min", 0.5)),
        context_exit_leader_macd_slope_max=float(cfg.get("context_exit_leader_macd_slope_max", 0.0)),
        context_exit_auto_leader_n=int(cfg.get("context_exit_auto_leader_n", 3)),
        context_exit_min_profit_to_arm=float(cfg.get("context_exit_min_profit_to_arm", 0.03)),
        context_exit_trail_drawdown_pct=float(cfg.get("context_exit_trail_drawdown_pct", 0.025)),
        entry_rsi_death_veto_enabled=int(cfg.get("entry_rsi_death_veto_enabled", 1)),
        entry_rsi_death_line=float(cfg.get("entry_rsi_death_line", 60)),
        entry_rsi_period=int(cfg.get("entry_rsi_period", 14)),
        entry_rsi_death_veto_skip_pre_cross=int(cfg.get("entry_rsi_death_veto_skip_pre_cross", 1)),
        floating_stop_loss_pct=float(cfg.get("floating_stop_loss_pct", 0.06)),
        floating_take_profit_pct=float(cfg.get("floating_take_profit_pct", 0.12)),
        floating_min_profit_to_trail=float(cfg.get("floating_min_profit_to_trail", 0.04)),
        floating_trail_drawdown_pct=float(cfg.get("floating_trail_drawdown_pct", 0.03)),
        context_leaders_by_concept=leaders_map,
        atr_stop_enabled=int(cfg.get("atr_stop_enabled", 0)),
        atr_stop_period=int(cfg.get("atr_stop_period", 20)),
        atr_stop_multiplier=float(cfg.get("atr_stop_multiplier", 1.5)),
        atr_stop_floor_pct=float(cfg.get("atr_stop_floor_pct", 0.03)),
        atr_stop_ceil_pct=float(cfg.get("atr_stop_ceil_pct", 0.08)),
        stop_close_verify=int(cfg.get("stop_close_verify", 0)),
        emergency_stop_pct=float(cfg.get("emergency_stop_pct", 0.10)),
    )


def effective_trade_cfg(
    trade_cfg: TradeExecutionConfig,
    active_mode: str,
    raw_trade_dict: dict,
) -> TradeExecutionConfig:
    """v4_pure 时把 trade.json.v4_overrides 合并到 trade_cfg, 返回新实例。
    覆盖字段: hard_stop / tp1 / tp2 / floating_*。max_holding_days 由 effective_max_hold_days 单独取。"""
    if active_mode != "v4_pure" or not raw_trade_dict:
        return trade_cfg
    merged = apply_mode_overrides(raw_trade_dict, active_mode)
    out = replace(trade_cfg)
    float_keys = (
        "hard_stop_loss_pct",
        "tp1_pct",
        "tp2_pct",
        "floating_stop_loss_pct",
        "floating_take_profit_pct",
        "floating_min_profit_to_trail",
        "floating_trail_drawdown_pct",
        "atr_stop_multiplier",
        "atr_stop_floor_pct",
        "atr_stop_ceil_pct",
        "emergency_stop_pct",
    )
    int_keys = (
        "atr_stop_enabled",
        "atr_stop_period",
        "stop_close_verify",
    )
    for k in float_keys:
        if k in merged:
            try:
                setattr(out, k, float(merged[k]))
            except (TypeError, ValueError):
                pass
    for k in int_keys:
        if k in merged:
            try:
                setattr(out, k, int(merged[k]))
            except (TypeError, ValueError):
                pass
    return out


def effective_max_hold_days(default_value: int, active_mode: str, raw_trade_dict: dict) -> int:
    """v4_pure 模式下尊重 trade.json.v4_overrides.max_holding_days(默认 20)。"""
    if active_mode != "v4_pure" or not raw_trade_dict:
        return int(default_value)
    merged = apply_mode_overrides(raw_trade_dict, active_mode)
    try:
        return int(merged.get("max_holding_days", default_value))
    except (TypeError, ValueError):
        return int(default_value)


def load_regime_cross_flow_overrides(path: str = "config/cross_flow_regimes.json") -> Dict[str, Dict]:
    """按牛/震荡/熊读取 cross_flow 增量覆盖；文件不存在则返回空。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"警告: {path} 格式错误，已忽略分状态覆盖。")
        return {}
    out: Dict[str, Dict] = {}
    for key in ("bull", "range", "bear"):
        raw = data.get(key, {})
        if isinstance(raw, dict):
            out[key] = {str(k): v for k, v in raw.items()}
    return out


def load_floating_pnl_regime_overrides(path: str = "config/floating_pnl_regimes.json") -> Dict[str, Dict]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"警告: {path} 格式错误，已忽略 floating_pnl 分状态覆盖。")
        return {}
    out: Dict[str, Dict] = {}
    for key in ("bull", "range", "bear"):
        raw = data.get(key, {})
        if isinstance(raw, dict):
            out[key] = {str(k): v for k, v in raw.items()}
    return out


def load_dynamic_decay_profile(
    profile_name: str,
    profile_config_path: str = "config/backtest_dynamic_decay_frozen.json",
) -> Dict[str, object]:
    """
    读取 dynamic_decay 运行档位参数。
    - mainline: 使用 frozen.params
    - aggressive: 使用 frozen.alternatives 中的进攻档（优先 dynamic_decay_fast250_aggressive）
    """
    if not profile_name:
        return {}
    p = Path(profile_config_path)
    if not p.exists():
        raise ValueError(f"profile 配置文件不存在: {profile_config_path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"profile 配置文件解析失败: {profile_config_path}") from exc

    if profile_name == "mainline":
        params = data.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("profile 配置格式错误: params 不是对象。")
        return params

    if profile_name == "aggressive":
        alts = data.get("alternatives", [])
        if not isinstance(alts, list):
            raise ValueError("profile 配置格式错误: alternatives 不是数组。")
        preferred = None
        fallback = None
        for item in alts:
            if not isinstance(item, dict):
                continue
            if fallback is None:
                fallback = item
            if str(item.get("name", "")).strip() == "dynamic_decay_fast250_aggressive":
                preferred = item
                break
        target = preferred or fallback
        if not target:
            raise ValueError("profile=aggressive 未找到可用 alternatives。")
        out = dict(target)
        out.pop("name", None)
        out.pop("evidence_file", None)
        return out

    raise ValueError("profile 仅支持 mainline 或 aggressive。")


def merge_floating_cfg_for_regime(trade_cfg: TradeExecutionConfig, regime: str, overrides_root: Dict[str, Dict]) -> Dict[str, float]:
    base = {
        "floating_stop_loss_pct": float(trade_cfg.floating_stop_loss_pct),
        "floating_take_profit_pct": float(trade_cfg.floating_take_profit_pct),
        "floating_min_profit_to_trail": float(trade_cfg.floating_min_profit_to_trail),
        "floating_trail_drawdown_pct": float(trade_cfg.floating_trail_drawdown_pct),
    }
    ov = overrides_root.get(regime, {})
    for k in list(base.keys()):
        if k in ov:
            try:
                base[k] = float(ov[k])
            except (TypeError, ValueError):
                pass
    return base


def merge_cross_flow_for_regime(base: dict, regime: str, overrides_root: Dict[str, Dict]) -> dict:
    merged = dict(base)
    merged.update(overrides_root.get(regime, {}))
    return merged


def rotation_floor_from_cross(cross_eff: dict, regime: str) -> float:
    """与 v0.5 cross_flow.json 默认对齐: 不要求板块龙头, range/bear 放宽到 30/35。"""
    if regime == "bull":
        return float(cross_eff.get("bull_min_rotation_score", 35.0))
    if regime == "range":
        return float(cross_eff.get("range_min_rotation_score", 30.0))
    return float(cross_eff.get("bear_min_rotation_score", 35.0))


def entry_score_pref_min_from_cross(cross_eff: dict, regime: str) -> int:
    """每期入选股票的 entry_signal_score 下限偏好。"""
    default = 1 if regime == "bull" else (3 if regime == "range" else 2)
    return int(cross_eff.get("entry_score_pref_min", default))


def _concept_rotation_resolved_for_gate(concept_rotation_score: Optional[float]) -> float:
    """searchv1 合并板块轮动分后对缺失值 fillna(50)，rotation_ok 判断与此对齐。"""
    if concept_rotation_score is None or pd.isna(concept_rotation_score):
        return 50.0
    return float(concept_rotation_score)


def _finalize_entry_signal_score(
    technical_score: int,
    cross_eff: dict,
    regime: str,
    concept_rotation_score: Optional[float],
) -> int:
    """应用 cross_flow 轮动相关入场闸（与 searchv1 rotation_entry_hard_gate / rotation_extreme_weak_floor 对齐）。"""
    if technical_score <= 0:
        return 0
    if int(cross_eff.get("rotation_entry_hard_gate", 0)) == 1:
        cr = _concept_rotation_resolved_for_gate(concept_rotation_score)
        rot_min = rotation_floor_from_cross(cross_eff, regime)
        if cr < rot_min:
            return 0
    ref_floor = float(cross_eff.get("rotation_extreme_weak_floor", -1.0))
    if ref_floor >= 0.0:
        if concept_rotation_score is not None and pd.notna(concept_rotation_score):
            if float(concept_rotation_score) < ref_floor:
                return 0
    return technical_score


def concept_rotation_score_for_code(ranked: pd.DataFrame, ts_code: str) -> Optional[float]:
    """从当期横截面表取个股板块轮动分；无列或缺失则返回 None（硬门槛侧按 50 处理）。"""
    if "concept_rotation_score" not in ranked.columns:
        return None
    if ts_code not in ranked.index:
        return None
    v = ranked.loc[ts_code, "concept_rotation_score"]
    return float(v) if pd.notna(v) else None


class ThrottledFetcher:
    def __init__(self, token: str, throttle: ThrottleConfig):
        self.fetcher = DataFetcher(token=token)
        self.throttle = throttle
        self._count = 0

    def _tick(self):
        self._count += 1
        time.sleep(self.throttle.per_request_sleep)
        if self._count % self.throttle.burst_requests == 0:
            time.sleep(self.throttle.burst_sleep)

    def get_index_daily(self, *args, **kwargs):
        self._tick()
        return self.fetcher.get_index_daily(*args, **kwargs)

    def get_latest_trade_date(self, *args, **kwargs):
        self._tick()
        return self.fetcher.get_latest_trade_date(*args, **kwargs)

    def get_stock_basic_mainboard(self, *args, **kwargs):
        self._tick()
        return self.fetcher.get_stock_basic_mainboard(*args, **kwargs)

    def get_theme_pool(self, *args, **kwargs):
        self._tick()
        return self.fetcher.get_theme_pool(*args, **kwargs)

    def get_stock_daily(self, *args, **kwargs):
        self._tick()
        return self.fetcher.get_stock_daily(*args, **kwargs)

    def get_daily_basic(self, *args, **kwargs):
        self._tick()
        return self.fetcher.get_daily_basic(*args, **kwargs)


def load_or_fetch_daily(
    tf: ThrottledFetcher,
    ts_code: str,
    start_date: str,
    end_date: str,
    cache_dir: Path,
) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{ts_code}_{start_date}_{end_date}.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["trade_date"])
        return df.set_index("trade_date").sort_index()

    df = tf.get_stock_daily(ts_code, start_date, end_date)
    if not df.empty:
        out = df.reset_index().rename(columns={"index": "trade_date"})
        out.to_csv(cache_file, index=False, encoding="utf-8-sig")
    return df


def calc_stock_features_asof(df: pd.DataFrame) -> Dict[str, float]:
    if len(df) < 80:
        return {
            "momentum_20d": np.nan,
            "breakout_readiness": np.nan,
            "ma_structure": np.nan,
            "volatility_30d": np.nan,
        }

    close = df["close"]
    vol = df["volume"]

    # momentum_20d
    momentum_20d = float(close.iloc[-1] / close.iloc[-21] - 1)

    # breakout_readiness
    ma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    upper = ma20 + 2 * std20
    lower = ma20 - 2 * std20
    band_width = (upper - lower) / ma20
    width_score = 1 - np.clip(float(band_width.iloc[-1]), 0, 0.25) / 0.25

    resistance_20 = close.rolling(20).max().iloc[-2]
    near_break = np.clip((close.iloc[-1] / resistance_20 - 0.97) / 0.03, 0, 1)
    vol_ratio = vol.rolling(5).mean().iloc[-1] / max(vol.rolling(20).mean().iloc[-1], 1)
    vol_score = np.clip((vol_ratio - 0.8) / 0.7, 0, 1)
    breakout_readiness = 100 * (0.4 * width_score + 0.4 * near_break + 0.2 * vol_score)

    # ma_structure
    ma5 = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20_v = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    ma_structure = 0.0
    if ma5 > ma10:
        ma_structure += 30
    if ma10 > ma20_v:
        ma_structure += 30
    if ma20_v > ma60:
        ma_structure += 40

    # volatility_30d
    ret30 = close.pct_change().dropna().tail(30)
    volatility_30d = float(ret30.std() * np.sqrt(252))

    return {
        "momentum_20d": momentum_20d,
        "breakout_readiness": breakout_readiness,
        "ma_structure": ma_structure,
        "volatility_30d": volatility_30d,
    }


def calc_setup_features_asof(df: pd.DataFrame) -> Dict[str, float]:
    if len(df) < 80:
        return {
            "runup_10d": np.nan,
            "box_position_60d": np.nan,
            "accumulation_score": np.nan,
            "consolidation_breakout_score": np.nan,
        }
    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"]

    runup_10d = float(close.iloc[-1] / close.iloc[-11] - 1) if len(close) >= 12 else np.nan

    h60 = float(high.tail(60).max())
    l60 = float(low.tail(60).min())
    box_pos = (float(close.iloc[-1]) - l60) / (h60 - l60) if h60 > l60 else np.nan

    direction = np.sign(close.diff().fillna(0))
    obv = (direction * vol).cumsum()
    if len(obv) >= 20:
        y = obv.tail(20).values
        x = np.arange(len(y))
        obv_slope = np.polyfit(x, y, 1)[0]
        base = max(abs(obv.tail(20).mean()), 1.0)
        obv_slope_n = obv_slope / base * 100
    else:
        obv_slope_n = np.nan
    vol_contract = float(vol.tail(5).mean() / max(vol.tail(20).mean(), 1)) if len(vol) >= 20 else np.nan
    obv_score = 50 if pd.isna(obv_slope_n) else np.clip(50 + obv_slope_n * 8, 0, 100)
    vc_score = 50 if pd.isna(vol_contract) else np.clip(100 - np.clip((vol_contract - 0.85) / 0.6, 0, 1) * 100, 0, 100)
    box_score = 50 if pd.isna(box_pos) else np.clip(100 - box_pos * 100, 0, 100)
    accumulation_score = float(obv_score * 0.45 + vc_score * 0.25 + box_score * 0.30)

    # consolidation breakout
    h20 = float(high.tail(20).max())
    l20 = float(low.tail(20).min())
    c20 = float(close.tail(20).mean())
    amp20 = (h20 - l20) / c20 if c20 > 0 and h20 > l20 else np.nan
    amp_score = 50 if pd.isna(amp20) else (1 - np.clip((amp20 - 0.04) / 0.12, 0, 1)) * 100
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr5 = float(tr.tail(5).mean())
    atr20 = float(tr.tail(20).mean())
    atr_ratio = atr5 / atr20 if atr20 > 0 else np.nan
    atr_score = 50 if pd.isna(atr_ratio) else (1 - np.clip((atr_ratio - 0.75) / 0.5, 0, 1)) * 100
    h40 = float(high.tail(40).max())
    near = float(close.iloc[-1] / h40) if h40 > 0 else np.nan
    near_score = 50 if pd.isna(near) else np.clip((near - 0.93) / 0.07, 0, 1) * 100
    vol_ratio = float(vol.tail(5).mean() / max(vol.tail(20).mean(), 1)) if len(vol) >= 20 else np.nan
    if pd.isna(vol_ratio):
        vol_score = 50
    elif vol_ratio < 0.9:
        vol_score = np.clip(vol_ratio / 0.9, 0, 1) * 70
    elif vol_ratio <= 1.25:
        vol_score = 70 + np.clip((vol_ratio - 0.9) / 0.35, 0, 1) * 30
    else:
        vol_score = max(0, 100 - (vol_ratio - 1.25) * 120)
    consolidation_breakout_score = float(amp_score * 0.35 + atr_score * 0.30 + near_score * 0.20 + vol_score * 0.15)

    return {
        "runup_10d": runup_10d,
        "box_position_60d": box_pos,
        "accumulation_score": accumulation_score,
        "consolidation_breakout_score": consolidation_breakout_score,
    }


def calc_imminent_features_asof(df: pd.DataFrame) -> Dict[str, float]:
    """
    回测版「突破前夜」5 因子近似(对齐 searchv1 v0.5):
      - volume_contraction_score: 5d/20d 量比, 区间 [0.7,1.0] 满分
      - amp_contraction_pct:      近5日振幅在过去60日的分位 (越低越好)
      - box_score:                box_position_60d ∈ [0.45,0.75] 满分
      - ma_cluster_tightness:     MA5/10/20 三线最大相对距离, 越粘合分越高
      - wr_trend_score:           W&R 趋势分(level/rel/down 加权)
    资金 mf_pos_days 在回测中无可用接口, 取中性 50, 占 0.15 权重不变。
    """
    out = {
        "volume_contraction_score": np.nan,
        "amp_contraction_pct": np.nan,
        "ma_cluster_tightness": np.nan,
        "wr_trend_score": np.nan,
        "imminent_score": np.nan,
    }
    if len(df) < 80:
        return out
    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"]

    vol5 = float(vol.tail(5).mean())
    vol20 = float(vol.tail(20).mean())
    if vol20 > 0:
        ratio = vol5 / vol20
        if ratio <= 0.5 or ratio >= 1.6:
            s_vol = 0.0
        elif 0.7 <= ratio <= 1.0:
            s_vol = 100.0
        elif 0.5 < ratio < 0.7:
            s_vol = float(np.clip((ratio - 0.5) / 0.2 * 100, 0, 100))
        else:
            s_vol = float(np.clip((1.6 - ratio) / 0.6 * 100, 0, 100))
    else:
        s_vol = 50.0
    out["volume_contraction_score"] = s_vol

    amp = ((high - low) / close.replace(0, np.nan)).dropna()
    if len(amp) >= 65:
        recent5 = float(amp.tail(5).mean())
        hist60 = amp.tail(65).head(60)
        amp_pct = float((hist60 <= recent5).mean()) if not hist60.empty else 0.5
        s_amp = (1.0 - amp_pct) * 100.0
    else:
        amp_pct = np.nan
        s_amp = 50.0
    out["amp_contraction_pct"] = amp_pct

    h60 = float(high.tail(60).max())
    l60 = float(low.tail(60).min())
    if h60 > l60:
        box = (float(close.iloc[-1]) - l60) / (h60 - l60)
    else:
        box = np.nan
    if pd.isna(box):
        s_box = 50.0
    elif 0.45 <= box <= 0.75:
        s_box = 100.0
    elif 0.30 <= box < 0.45:
        s_box = float(np.clip((box - 0.30) / 0.15 * 100, 0, 100))
    elif 0.75 < box <= 0.90:
        s_box = float(np.clip((0.90 - box) / 0.15 * 100, 0, 100))
    else:
        s_box = 0.0

    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    if ma20 > 0 and not any(pd.isna(x) for x in (ma5, ma10, ma20)):
        spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20
        s_ma = float(np.clip((0.03 - spread) / 0.03 * 100, 0, 100))
    else:
        s_ma = 50.0
    out["ma_cluster_tightness"] = s_ma

    feats = _cross_wr_features(df.tail(60))
    s_wr = float(feats.get("wr_trend_score", 50.0)) if pd.notna(feats.get("wr_trend_score", np.nan)) else 50.0
    out["wr_trend_score"] = s_wr

    s_mf = 50.0
    score = s_vol * 0.20 + s_amp * 0.15 + s_box * 0.15 + s_ma * 0.20 + s_wr * 0.15 + s_mf * 0.15
    out["imminent_score"] = float(np.clip(score, 0.0, 100.0))
    return out


def normalize_xs(series: pd.Series) -> pd.Series:
    x = series.dropna()
    if x.empty:
        return pd.Series(50.0, index=series.index)
    low = x.quantile(0.01)
    high = x.quantile(0.99)
    x = x.clip(low, high)
    std = x.std()
    if std == 0 or pd.isna(std):
        return pd.Series(50.0, index=series.index)
    z = (x - x.mean()) / std
    score = (50 + 12 * z).clip(0, 100)
    out = pd.Series(50.0, index=series.index)
    out.loc[score.index] = score
    return out


def month_end_dates(index_df: pd.DataFrame) -> List[pd.Timestamp]:
    x = index_df.copy()
    x["ym"] = x.index.to_period("M")
    return [grp.index.max() for _, grp in x.groupby("ym")]


def rolling_rebalance_dates(index_df: pd.DataFrame, hold_days: int) -> List[pd.Timestamp]:
    """
    以交易日为基准，每 hold_days 天调仓一次。
    """
    dates = list(index_df.index.sort_values().unique())
    if hold_days <= 1:
        return dates
    return [dates[i] for i in range(0, len(dates), hold_days)]


def resolve_backtest_end_date(tf: "ThrottledFetcher", raw_end: str, lookback_days: int = 10) -> str:
    """
    解析可用回测结束日：
    1) 优先使用数据接口给出的最新交易日；
    2) 若该日期指数无数据，向前回退最多 lookback_days 天，避免周末/节假日导致空数据。
    """
    latest = tf.get_latest_trade_date(raw_end)
    try:
        candidate = datetime.strptime(latest, "%Y-%m-%d").date()
    except ValueError:
        candidate = datetime.strptime(raw_end, "%Y-%m-%d").date()

    for _ in range(max(1, lookback_days)):
        day = candidate.strftime("%Y-%m-%d")
        try:
            idx = tf.get_index_daily("000001.SH", day, day)
        except ValueError:
            idx = pd.DataFrame()
        if not idx.empty:
            return day
        candidate = candidate - timedelta(days=1)
    raise ValueError("无法解析可用回测结束日：最近交易日及回退窗口内均无指数数据。")


def get_regime_asof(index_hist: pd.DataFrame) -> str:
    detector = MarketRegimeDetector(
        {
            "sse": index_hist,
            "hs300": index_hist,
            "allshare": index_hist,
        }
    )
    return detector.regime()


def score_universe_asof(
    asof_date: pd.Timestamp,
    universe: List[str],
    stock_data: Dict[str, pd.DataFrame],
    daily_basic_asof: pd.DataFrame,
    regime: str,
    strategy_mode: str = "hybrid",
) -> pd.DataFrame:
    rows = []
    db = daily_basic_asof.set_index("ts_code") if not daily_basic_asof.empty else pd.DataFrame()
    for code in universe:
        hist = stock_data.get(code, pd.DataFrame())
        hist = hist[hist.index <= asof_date]
        feats = calc_stock_features_asof(hist)
        sfeat = calc_setup_features_asof(hist)
        ifeat = calc_imminent_features_asof(hist) if strategy_mode == "v4_pure" else {
            "volume_contraction_score": np.nan,
            "amp_contraction_pct": np.nan,
            "ma_cluster_tightness": np.nan,
            "wr_trend_score": np.nan,
            "imminent_score": np.nan,
        }
        pb = np.nan
        pe_ttm = np.nan
        if not db.empty and code in db.index:
            pb = float(db.loc[code].get("pb", np.nan))
            pe_ttm = float(db.loc[code].get("pe_ttm", np.nan))
        rows.append(
            {
                "ts_code": code,
                "momentum_20d": feats["momentum_20d"],
                "breakout_readiness": feats["breakout_readiness"],
                "ma_structure": feats["ma_structure"],
                "volatility_30d": feats["volatility_30d"],
                "runup_10d": sfeat["runup_10d"],
                "box_position_60d": sfeat["box_position_60d"],
                "accumulation_score": sfeat["accumulation_score"],
                "consolidation_breakout_score": sfeat["consolidation_breakout_score"],
                "imminent_score": ifeat["imminent_score"],
                "volume_contraction_score": ifeat["volume_contraction_score"],
                "amp_contraction_pct": ifeat["amp_contraction_pct"],
                "ma_cluster_tightness": ifeat["ma_cluster_tightness"],
                "wr_trend_score": ifeat["wr_trend_score"],
                "pb": pb,
                "pe_ttm": pe_ttm,
            }
        )
    raw = pd.DataFrame(rows).set_index("ts_code")

    if strategy_mode == "v4_pure":
        # v0.5 「盘整启动专用」公式: imminent 主导, 防追高(low_runup), 板块 rotation 后注入加权
        # 总和 1.0 = imminent 0.45 + accumulation 0.20 + consolidation_breakout 0.15
        #           + low_runup_10d 0.10 + low_volatility_30d 0.10
        config = [
            ("imminent_score", 0.45, "positive"),
            ("accumulation_score", 0.20, "positive"),
            ("consolidation_breakout_score", 0.15, "positive"),
            ("runup_10d", 0.10, "negative"),
            ("volatility_30d", 0.10, "negative"),
        ]
    elif regime == "bull":
        config = [
            ("breakout_readiness", 0.35, "positive"),
            ("momentum_20d", 0.30, "positive"),
            ("ma_structure", 0.20, "positive"),
            ("volatility_30d", 0.15, "negative"),
        ]
    elif regime == "bear":
        config = [
            ("breakout_readiness", 0.30, "positive"),
            ("volatility_30d", 0.30, "negative"),
            ("pb", 0.20, "negative"),
            ("ma_structure", 0.20, "positive"),
        ]
    else:
        config = [
            ("breakout_readiness", 0.35, "positive"),
            ("ma_structure", 0.25, "positive"),
            ("momentum_20d", 0.20, "positive"),
            ("volatility_30d", 0.10, "negative"),
            ("pe_ttm", 0.10, "negative"),
        ]

    score_df = pd.DataFrame(index=raw.index)
    total = pd.Series(0.0, index=raw.index)
    for name, w, direction in config:
        s = normalize_xs(raw[name])
        if direction == "negative":
            s = 100 - s
        score_df[name] = s
        total += s * w

    out = raw.copy()
    out["total_score"] = total
    out["regime"] = regime
    out["asof_date"] = asof_date
    return out.sort_values("total_score", ascending=False)


def next_trade_price(df: pd.DataFrame, after_date: pd.Timestamp) -> float:
    x = df[df.index > after_date]
    if x.empty:
        return np.nan
    return float(x.iloc[0]["open"])


def hold_to_date_price(df: pd.DataFrame, target_date: pd.Timestamp) -> float:
    x = df[df.index <= target_date]
    if x.empty:
        return np.nan
    return float(x.iloc[-1]["close"])


def apply_risk_filters(
    ranked: pd.DataFrame,
    risk_cfg: RiskControlConfig,
    active_mode: str = "hybrid",
) -> pd.DataFrame:
    """
    v0.5 拆分:
    - 通用过滤: volatility / breakout_readiness / turnover_rate
    - v4_pure 时额外收紧: runup_10d / box_position_60d 区间 / accumulation_score。
      用于「盘整启动专用」语义, 拒绝已启动股、拒绝高位股。
    """
    f = ranked.copy()
    f = f[f["volatility_30d"] <= risk_cfg.max_volatility_30d]
    f = f[f["breakout_readiness"] >= risk_cfg.min_breakout_readiness]
    if "turnover_rate" in f.columns:
        tr = pd.to_numeric(f["turnover_rate"], errors="coerce")
        keep_tr = tr.isna() | ((tr >= risk_cfg.min_turnover_rate) & (tr <= risk_cfg.max_turnover_rate))
        f = f[keep_tr]
    if active_mode == "v4_pure":
        runup_max = float(risk_cfg.max_runup_10d_pct) / 100.0
        box_lo = float(risk_cfg.box_low_position_min)
        box_hi = float(risk_cfg.box_low_position_max)
        accum_min = float(risk_cfg.min_accumulation_score)
        if "runup_10d" in f.columns:
            ru = pd.to_numeric(f["runup_10d"], errors="coerce")
            f = f[ru.isna() | (ru <= runup_max)]
        if "box_position_60d" in f.columns:
            bp = pd.to_numeric(f["box_position_60d"], errors="coerce")
            f = f[bp.isna() | ((bp >= box_lo) & (bp <= box_hi))]
        if "accumulation_score" in f.columns:
            ac = pd.to_numeric(f["accumulation_score"], errors="coerce")
            f = f[ac.isna() | (ac >= accum_min)]
    return f


def pick_with_industry_cap(ranked: pd.DataFrame, top_n: int, max_industry_ratio: float) -> List[str]:
    """按 total_score 取股, 同一申万一级行业最多 floor(top_n * max_industry_ratio) 只(下限 1)。

    v0.6 修复: 原实现 `getattr(row, "ts_code", None)` 在 ts_code 仅作为索引时永远返回 None,
    导致函数始终返回空列表,选股流程一直退化到末尾的 fallback `ranked.index[:1]`。
    """
    if ranked.empty:
        return []
    max_per_industry = max(1, int(np.floor(top_n * max_industry_ratio)))
    picked: List[str] = []
    industry_count: Dict[str, int] = {}

    for row in ranked.itertuples():
        industry = getattr(row, "industry", None)
        industry = industry if isinstance(industry, str) and industry else "未知行业"
        code = getattr(row, "ts_code", None)
        if not isinstance(code, str) or not code:
            idx_val = getattr(row, "Index", None)
            code = idx_val if isinstance(idx_val, str) else None
        if not code:
            continue
        if industry_count.get(industry, 0) >= max_per_industry:
            continue
        picked.append(code)
        industry_count[industry] = industry_count.get(industry, 0) + 1
        if len(picked) >= top_n:
            break
    return picked


def period_exit_with_stoploss(
    df: pd.DataFrame,
    rebalance_date: pd.Timestamp,
    next_date: pd.Timestamp,
    stop_loss_pct: float,
) -> tuple:
    trade_window = df[(df.index > rebalance_date) & (df.index <= next_date)]
    if trade_window.empty:
        return np.nan, np.nan, False
    entry = float(trade_window.iloc[0]["open"])
    if entry <= 0:
        return np.nan, np.nan, False

    stop_price = entry * (1 - stop_loss_pct)
    hit_stop = trade_window["low"] <= stop_price
    if hit_stop.any():
        return entry, stop_price, True
    return entry, float(trade_window.iloc[-1]["close"]), False


def _cross_wr_features(hist: pd.DataFrame, pre_cross_gap_shrink_ratio_max: float = 0.7) -> Dict[str, float]:
    close = hist["close"]
    high = hist["high"]
    low = hist["low"]

    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma5_now = float(ma5.iloc[-1])
    ma5_prev = float(ma5.iloc[-6])
    ma10_now = float(ma10.iloc[-1])
    ma10_prev = float(ma10.iloc[-2])
    ma5_slope_5d = (ma5_now / ma5_prev - 1) * 100 if ma5_prev != 0 else 0.0

    gap_now = ma10_now - ma5_now
    gap_prev = float(ma10.iloc[-4] - ma5.iloc[-4])
    gap_shrink_ratio = np.nan
    if gap_prev > 0:
        gap_shrink_ratio = gap_now / gap_prev

    is_just_cross = ma5_now >= ma10_now and ma5_prev < ma10_prev
    is_pre_cross = gap_now > 0 and pd.notna(gap_shrink_ratio) and gap_shrink_ratio <= float(pre_cross_gap_shrink_ratio_max)

    def _wr(period: int) -> float:
        hh = float(high.tail(period).max())
        ll = float(low.tail(period).min())
        c = float(close.iloc[-1])
        if hh <= ll:
            return np.nan
        return (hh - c) / (hh - ll) * 100

    wr_fast = _wr(14)
    wr_slow = _wr(28) if len(hist) >= 30 else np.nan

    wr_seq = []
    for i in range(3, -1, -1):
        sub = hist.iloc[: len(hist) - i] if i > 0 else hist
        if len(sub) < 16:
            continue
        hh = float(sub["high"].tail(14).max())
        ll = float(sub["low"].tail(14).min())
        c = float(sub["close"].iloc[-1])
        if hh > ll:
            wr_seq.append((hh - c) / (hh - ll) * 100)
    wr_down_days = int((pd.Series(wr_seq).diff() < 0).sum()) if len(wr_seq) >= 2 else 0

    level_score = np.clip((100 - wr_fast) / 80 * 100, 0, 100) if pd.notna(wr_fast) else 0
    rel_score = 50.0
    if pd.notna(wr_fast) and pd.notna(wr_slow):
        rel_score = np.clip(50 + (wr_slow - wr_fast) * 1.5, 0, 100)
    down_score = np.clip(wr_down_days / 3 * 100, 0, 100)
    wr_trend_score = float(level_score * 0.4 + rel_score * 0.3 + down_score * 0.3)

    return {
        "ma5_now": ma5_now,
        "ma5_slope_5d": ma5_slope_5d,
        "is_just_cross": bool(is_just_cross),
        "is_pre_cross": bool(is_pre_cross),
        "wr_trend_score": wr_trend_score,
    }


def _rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.inf)
    return 100 - 100 / (1 + rs)


def entry_signal_ok(
    df: pd.DataFrame,
    rebalance_date: pd.Timestamp,
    trade_cfg: TradeExecutionConfig,
    regime: str = "bull",
    cross_eff: Optional[dict] = None,
    concept_rotation_score: Optional[float] = None,
) -> bool:
    return (
        entry_signal_score(
            df,
            rebalance_date,
            trade_cfg,
            regime=regime,
            cross_eff=cross_eff,
            concept_rotation_score=concept_rotation_score,
        )
        >= 3
    )


def entry_signal_score(
    df: pd.DataFrame,
    rebalance_date: pd.Timestamp,
    trade_cfg: TradeExecutionConfig,
    regime: str = "bull",
    cross_eff: Optional[dict] = None,
    active_mode: str = "hybrid",
    risk_cfg: Optional[RiskControlConfig] = None,
    concept_rotation_score: Optional[float] = None,
) -> int:
    """
    入场打分 0~5。
    - hybrid 模式: 沿用 breakout/上MA5/MA5斜率/金叉/W&R 5 维评分(主升浪)。
    - v4_pure 模式(盘整启动专用): 启用 setup 通道 — box 在区间 + accumulation 高 + 量能温和 +
      MA 不走弱 + W&R 趋势分; 不再硬要求"昨日新高+1"才入场, 因为盘整启动通常还没真突破。
    - 若传入 concept_rotation_score，则在打分后按 cross_flow 应用 rotation_entry_hard_gate、
      rotation_extreme_weak_floor（与 searchv1 入场链一致）。
    """
    cross_eff = cross_eff or {}
    wr_min = float(cross_eff.get("wr_trend_score_min", 48.0))
    gap_max = float(cross_eff.get("pre_cross_gap_shrink_ratio_max", 0.7))
    rsi_veto_enabled = int(getattr(trade_cfg, "entry_rsi_death_veto_enabled", 1)) == 1
    rsi_veto_skip_pre_cross = int(getattr(trade_cfg, "entry_rsi_death_veto_skip_pre_cross", 1)) == 1
    rsi_line = float(getattr(trade_cfg, "entry_rsi_death_line", 60.0))
    rsi_period = max(6, int(getattr(trade_cfg, "entry_rsi_period", 14)))

    hist = df[df.index <= rebalance_date].tail(80)
    if len(hist) < 25:
        return 0
    close = float(hist["close"].iloc[-1])
    high_prev = float(hist["high"].iloc[-1])
    trigger = high_prev * (1 + trade_cfg.entry_breakout_buffer_pct / 100.0)
    feats = _cross_wr_features(hist, pre_cross_gap_shrink_ratio_max=gap_max)
    if rsi_veto_enabled:
        rsi = _rsi_series(hist["close"], period=rsi_period).dropna()
        if len(rsi) >= 2:
            rsi_death = float(rsi.iloc[-2]) > rsi_line >= float(rsi.iloc[-1])
            if rsi_death:
                if not (rsi_veto_skip_pre_cross and bool(feats.get("is_pre_cross", False))):
                    return 0
    ma5_now = feats["ma5_now"]
    ma5_slope_5d = feats["ma5_slope_5d"]
    if pd.isna(ma5_now):
        return 0

    breakout_ok = close >= trigger
    above_ma5_ok = close >= ma5_now * trade_cfg.entry_need_above_ma5
    slope_ok = ma5_slope_5d >= trade_cfg.entry_min_ma5_slope_5d
    cross_ok = feats["is_just_cross"] or feats["is_pre_cross"]
    wr_ok = feats["wr_trend_score"] >= wr_min

    if active_mode == "v4_pure":
        # 盘整启动专用 setup 通道
        rcfg = risk_cfg or RiskControlConfig(box_low_position_min=0.25, box_low_position_max=0.65, min_accumulation_score=55.0)
        sf = calc_setup_features_asof(hist) if len(hist) >= 80 else {"box_position_60d": np.nan, "accumulation_score": np.nan}
        box_pos = sf.get("box_position_60d", np.nan)
        accum = sf.get("accumulation_score", np.nan)
        box_ok = pd.notna(box_pos) and (rcfg.box_low_position_min <= box_pos <= rcfg.box_low_position_max)
        accum_ok = pd.notna(accum) and (accum >= rcfg.min_accumulation_score)
        slope_softer = ma5_slope_5d >= -0.5  # v4 放宽: MA5 5日不大幅下倾即可
        # 5 维评分: box / accum / 量价不弱(slope_softer) / 金叉前置或刚金叉 / W&R
        score_v4 = (
            (1 if box_ok else 0)
            + (1 if accum_ok else 0)
            + (1 if slope_softer else 0)
            + (1 if (cross_ok or above_ma5_ok) else 0)
            + (1 if wr_ok else 0)
        )
        # bear 市仍极严: 必须 box + accum + W&R 同时满足
        if regime == "bear":
            if not (box_ok and accum_ok and wr_ok):
                return 0
        return _finalize_entry_signal_score(score_v4, cross_eff, regime, concept_rotation_score)

    score = 0
    score += 1 if breakout_ok else 0
    score += 1 if above_ma5_ok else 0
    score += 1 if slope_ok else 0
    score += 1 if cross_ok else 0
    score += 1 if wr_ok else 0

    if regime == "bull":
        return _finalize_entry_signal_score(score, cross_eff, regime, concept_rotation_score)
    if regime == "range":
        if not feats["is_just_cross"] and not breakout_ok:
            return 0
        return _finalize_entry_signal_score(score, cross_eff, regime, concept_rotation_score)
    if feats["is_just_cross"] and wr_ok and breakout_ok and above_ma5_ok:
        return _finalize_entry_signal_score(score, cross_eff, regime, concept_rotation_score)
    return 0


def _wilder_atr(hist: pd.DataFrame, period: int = 20) -> float:
    """Wilder smoothed ATR, 返回最新一根 K 线的 ATR 绝对值。
    样本不足或缺列时返回 NaN, 调用方需做 NaN 兜底。
    """
    needed = {"high", "low", "close"}
    if hist is None or hist.empty or not needed.issubset(hist.columns):
        return float("nan")
    if len(hist) < period + 1:
        return float("nan")
    h = hist["high"].astype(float).to_numpy()
    l = hist["low"].astype(float).to_numpy()
    c = hist["close"].astype(float).to_numpy()
    prev_c = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    tr = tr[1:]
    if tr.size < period:
        return float("nan")
    alpha = 1.0 / period
    atr = tr[0]
    for x in tr[1:]:
        atr = alpha * x + (1 - alpha) * atr
    return float(atr) if np.isfinite(atr) else float("nan")


def _resolve_stop_pct(
    df_pre: pd.DataFrame, entry_price: float, trade_cfg: TradeExecutionConfig
) -> float:
    """根据 trade_cfg 决定有效止损百分比。
    atr_stop_enabled=0 -> hard_stop_loss_pct
    atr_stop_enabled=1 -> clip(k*ATR/entry, [floor, ceil]); ATR 缺失则回退到 hard_stop_loss_pct
    """
    base = float(trade_cfg.hard_stop_loss_pct)
    if int(getattr(trade_cfg, "atr_stop_enabled", 0)) != 1:
        return base
    period = int(getattr(trade_cfg, "atr_stop_period", 20))
    atr = _wilder_atr(df_pre, period=period)
    if not np.isfinite(atr) or atr <= 0 or entry_price <= 0:
        return base
    mult = float(getattr(trade_cfg, "atr_stop_multiplier", 1.5))
    floor = float(getattr(trade_cfg, "atr_stop_floor_pct", 0.03))
    ceil = float(getattr(trade_cfg, "atr_stop_ceil_pct", 0.08))
    return float(np.clip(mult * atr / entry_price, floor, ceil))


def period_exit_with_trade_plan(
    df: pd.DataFrame,
    rebalance_date: pd.Timestamp,
    next_date: pd.Timestamp,
    trade_cfg: TradeExecutionConfig,
) -> tuple:
    """v0.6 P1: 止损逻辑三层
    1) emergency_stop_pct: 当日 low 跌穿 (默认 -10%) 立即按 emergency_stop_price 平仓 (黑天鹅护栏)
    2) 主止损价 stop_price: ATR 动态(若开启) 或 固定 hard_stop_loss_pct
       - stop_close_verify=0 (旧行为): low <= stop_price 即出, 实测易被插针打掉
       - stop_close_verify=1: 仅当 close <= stop_price 时按 close 价出
    3) tp2 / tp1 维持原半仓混合出场
    """
    trade_window = df[(df.index > rebalance_date) & (df.index <= next_date)]
    if trade_window.empty:
        return np.nan, np.nan, "none"
    entry = float(trade_window.iloc[0]["open"])
    if entry <= 0:
        return np.nan, np.nan, "none"

    df_pre = df[df.index <= rebalance_date]
    eff_stop_pct = _resolve_stop_pct(df_pre, entry, trade_cfg)
    stop_price = entry * (1 - eff_stop_pct)
    emergency_pct = float(getattr(trade_cfg, "emergency_stop_pct", 0.10))
    emergency_price = entry * (1 - emergency_pct)
    close_verify = int(getattr(trade_cfg, "stop_close_verify", 0)) == 1
    tp1 = entry * (1 + trade_cfg.tp1_pct)
    tp2 = entry * (1 + trade_cfg.tp2_pct)

    for _, row in trade_window.iterrows():
        low = float(row["low"])
        high = float(row["high"])
        close = float(row["close"])
        if low <= emergency_price:
            return entry, emergency_price, "stop"
        if close_verify:
            if close <= stop_price:
                return entry, close, "stop"
        else:
            if low <= stop_price:
                return entry, stop_price, "stop"
        if high >= tp2:
            return entry, tp2, "tp2"
        if high >= tp1:
            blended = 0.5 * tp1 + 0.5 * close
            return entry, blended, "tp1"

    return entry, float(trade_window.iloc[-1]["close"]), "close"


def period_exit_discretionary(
    df: pd.DataFrame,
    rebalance_date: pd.Timestamp,
    next_date: pd.Timestamp,
    trade_cfg: TradeExecutionConfig,
) -> tuple:
    """
    贴近主观执行：
    - 不使用硬止损；
    - 保留分档止盈；
    - 未触发止盈则按周期末收盘离场。
    """
    trade_window = df[(df.index > rebalance_date) & (df.index <= next_date)]
    if trade_window.empty:
        return np.nan, np.nan, "none"
    entry = float(trade_window.iloc[0]["open"])
    if entry <= 0:
        return np.nan, np.nan, "none"

    tp1 = entry * (1 + trade_cfg.tp1_pct)
    tp2 = entry * (1 + trade_cfg.tp2_pct)
    for _, row in trade_window.iterrows():
        high = float(row["high"])
        if high >= tp2:
            return entry, tp2, "tp2"
        if high >= tp1:
            close_now = float(row["close"])
            blended = 0.5 * tp1 + 0.5 * close_now
            return entry, blended, "tp1"

    return entry, float(trade_window.iloc[-1]["close"]), "close"


def _macd_hist_slope_recent(hist: pd.DataFrame, n: int = 5) -> float:
    return macd_hist_slope_recent(hist, n=n)


def period_exit_discretionary_decay(
    df: pd.DataFrame,
    rebalance_date: pd.Timestamp,
    next_date: pd.Timestamp,
    trade_cfg: TradeExecutionConfig,
    cross_eff: Optional[dict] = None,
) -> tuple:
    """
    主观执行 + 信号衰减离场：
    - 保留 TP1/TP2；
    - 不硬止损；
    - 若出现明显转弱（MACD柱体转弱或失守MA5且MA5走平/走弱）提前收盘离场。
    - cross_eff.decay_exit_require_ma_and_macd=1 时须 MA 与 MACD 同时转弱才衰减离场。
    """
    cross_eff = cross_eff or {}
    decay_both = int(cross_eff.get("decay_exit_require_ma_and_macd", 0)) == 1
    trade_window = df[(df.index > rebalance_date) & (df.index <= next_date)]
    if trade_window.empty:
        return np.nan, np.nan, "none"
    entry = float(trade_window.iloc[0]["open"])
    if entry <= 0:
        return np.nan, np.nan, "none"

    tp1 = entry * (1 + trade_cfg.tp1_pct)
    tp2 = entry * (1 + trade_cfg.tp2_pct)
    for dt, row in trade_window.iterrows():
        high = float(row["high"])
        if high >= tp2:
            return entry, tp2, "tp2"
        if high >= tp1:
            close_now = float(row["close"])
            blended = 0.5 * tp1 + 0.5 * close_now
            return entry, blended, "tp1"

        hist = df[df.index <= dt].tail(60)
        if len(hist) < 30:
            continue
        ma5 = hist["close"].rolling(5).mean()
        ma5_now = float(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else np.nan
        ma5_prev = float(ma5.iloc[-6]) if len(ma5.dropna()) >= 6 else np.nan
        ma5_slope_5d = (ma5_now / ma5_prev - 1) * 100 if pd.notna(ma5_now) and pd.notna(ma5_prev) and ma5_prev != 0 else np.nan
        macd_slope = _macd_hist_slope_recent(hist, n=5)
        close_now = float(hist["close"].iloc[-1])
        weak_ma = pd.notna(ma5_now) and pd.notna(ma5_slope_5d) and close_now < ma5_now and ma5_slope_5d <= 0
        weak_macd = pd.notna(macd_slope) and macd_slope < 0
        if decay_both:
            if weak_ma and weak_macd:
                return entry, close_now, "decay"
        elif weak_ma or weak_macd:
            return entry, close_now, "decay"

    return entry, float(trade_window.iloc[-1]["close"]), "close"


def _recent_return(df: pd.DataFrame, asof_date: pd.Timestamp, lookback_days: int) -> float:
    hist = df[df.index <= asof_date].tail(max(lookback_days + 1, 2))
    if len(hist) < lookback_days + 1:
        return np.nan
    base = float(hist["close"].iloc[-lookback_days - 1])
    now = float(hist["close"].iloc[-1])
    if base <= 0:
        return np.nan
    return now / base - 1.0


def _resolve_leaders_for_concept(
    concept_name: str,
    concept_members: List[str],
    asof_date: pd.Timestamp,
    all_data: Dict[str, pd.DataFrame],
    trade_cfg: TradeExecutionConfig,
) -> List[str]:
    cfg_map = trade_cfg.context_leaders_by_concept or {}
    if concept_name in cfg_map and cfg_map[concept_name]:
        return [c for c in cfg_map[concept_name] if c in all_data]

    auto_n = max(1, int(trade_cfg.context_exit_auto_leader_n))
    scored = []
    for code in concept_members:
        df = all_data.get(code)
        if df is None or df.empty:
            continue
        hist = df[df.index <= asof_date].tail(30)
        if len(hist) < 10:
            continue
        amount_proxy = float((hist["close"] * hist["volume"]).mean())
        scored.append((code, amount_proxy))
    if not scored:
        return []
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored[:auto_n]]


def period_exit_discretionary_context(
    code: str,
    df: pd.DataFrame,
    concept_name: str,
    concept_members_map: Dict[str, List[str]],
    all_data: Dict[str, pd.DataFrame],
    rebalance_date: pd.Timestamp,
    next_date: pd.Timestamp,
    trade_cfg: TradeExecutionConfig,
) -> tuple:
    """
    主观上下文离场：
    1) 保留 TP1/TP2；
    2) 双条件触发提前离场（板块资金撤离 + 龙头MACD收敛）。
    """
    _ = code
    trade_window = df[(df.index > rebalance_date) & (df.index <= next_date)]
    if trade_window.empty:
        return np.nan, np.nan, "none"
    entry = float(trade_window.iloc[0]["open"])
    if entry <= 0:
        return np.nan, np.nan, "none"

    tp1 = entry * (1 + trade_cfg.tp1_pct)
    tp2 = entry * (1 + trade_cfg.tp2_pct)
    require_both = int(trade_cfg.context_exit_require_both) == 1
    only_when_profit = int(trade_cfg.context_exit_only_when_profit) == 1
    min_hold_days = max(0, int(trade_cfg.context_exit_min_hold_days))
    confirm_days = max(1, int(trade_cfg.context_exit_confirm_days))
    min_profit_to_arm = max(0.0, float(trade_cfg.context_exit_min_profit_to_arm))
    trail_drawdown_pct = max(0.0, float(trade_cfg.context_exit_trail_drawdown_pct))
    sector_lb = max(2, int(trade_cfg.context_exit_sector_lookback_days))
    concept_members = concept_members_map.get(concept_name, [])
    trigger_streak = 0
    armed = False
    peak_price = entry

    for idx_day, (dt, row) in enumerate(trade_window.iterrows(), start=1):
        high = float(row["high"])
        if high >= tp2:
            return entry, tp2, "tp2"
        if high >= tp1:
            close_now = float(row["close"])
            blended = 0.5 * tp1 + 0.5 * close_now
            return entry, blended, "tp1"
        close_now = float(row["close"])
        peak_price = max(peak_price, float(row["high"]), close_now)
        if not armed and peak_price / entry - 1.0 >= min_profit_to_arm:
            armed = True
        if armed and trail_drawdown_pct > 0:
            drawdown_from_peak = 1.0 - (close_now / peak_price) if peak_price > 0 else 0.0
            if drawdown_from_peak >= trail_drawdown_pct:
                return entry, close_now, "trail_exit"

        sector_out = False
        if concept_members:
            rets = []
            for m in concept_members:
                sdf = all_data.get(m)
                if sdf is None or sdf.empty:
                    continue
                r = _recent_return(sdf, dt, sector_lb)
                if pd.notna(r):
                    rets.append(float(r))
            if rets:
                arr = np.array(rets, dtype=float)
                mean_ret = float(arr.mean())
                neg_ratio = float((arr < 0).mean())
                sector_out = (
                    mean_ret <= float(trade_cfg.context_exit_sector_ret_mean_max)
                    and neg_ratio >= float(trade_cfg.context_exit_sector_negative_ratio_min)
                )

        leader_weak = False
        leaders = _resolve_leaders_for_concept(concept_name, concept_members, dt, all_data, trade_cfg)
        if leaders:
            weak_cnt = 0
            valid_cnt = 0
            for ld in leaders:
                ldf = all_data.get(ld)
                if ldf is None or ldf.empty:
                    continue
                hist = ldf[ldf.index <= dt].tail(60)
                macd_slope = _macd_hist_slope_recent(hist, n=5)
                if pd.isna(macd_slope):
                    continue
                valid_cnt += 1
                if macd_slope <= float(trade_cfg.context_exit_leader_macd_slope_max):
                    weak_cnt += 1
            if valid_cnt >= int(trade_cfg.context_exit_leader_count_min):
                leader_weak = (weak_cnt / valid_cnt) >= float(trade_cfg.context_exit_leader_weak_ratio_min)

        trigger = (sector_out and leader_weak) if require_both else (sector_out or leader_weak)
        if only_when_profit:
            if close_now <= entry:
                trigger = False
        if idx_day <= min_hold_days:
            trigger_streak = 0
            continue
        if trigger:
            trigger_streak += 1
        else:
            trigger_streak = 0
        if trigger_streak >= confirm_days:
            return entry, float(row["close"]), "context_exit"

    return entry, float(trade_window.iloc[-1]["close"]), "close"


def period_exit_floating_pnl(
    df: pd.DataFrame,
    rebalance_date: pd.Timestamp,
    next_date: pd.Timestamp,
    trade_cfg: TradeExecutionConfig,
    floating_eff: Optional[dict] = None,
) -> tuple:
    """
    浮动盈亏驱动离场（hold_days 仅作为最大持仓上限）：
    - 跌破浮动止损离场；
    - 到达固定止盈离场；
    - 达到最小浮盈后启动移动保护，按回撤离场；
    - 未触发则在窗口末收盘离场。
    """
    trade_window = df[(df.index > rebalance_date) & (df.index <= next_date)]
    if trade_window.empty:
        return np.nan, np.nan, "none"
    entry = float(trade_window.iloc[0]["open"])
    if entry <= 0:
        return np.nan, np.nan, "none"

    floating_eff = floating_eff or {}
    stop_loss_pct = max(0.0, float(floating_eff.get("floating_stop_loss_pct", trade_cfg.floating_stop_loss_pct)))
    tp_pct = max(0.0, float(floating_eff.get("floating_take_profit_pct", trade_cfg.floating_take_profit_pct)))
    arm_pct = max(0.0, float(floating_eff.get("floating_min_profit_to_trail", trade_cfg.floating_min_profit_to_trail)))
    trail_dd = max(0.0, float(floating_eff.get("floating_trail_drawdown_pct", trade_cfg.floating_trail_drawdown_pct)))

    stop_price = entry * (1 - stop_loss_pct)
    tp_price = entry * (1 + tp_pct)
    peak_price = entry
    armed = False

    for _, row in trade_window.iterrows():
        low = float(row["low"])
        high = float(row["high"])
        close_now = float(row["close"])
        if low <= stop_price:
            return entry, stop_price, "floating_stop"
        if high >= tp_price:
            return entry, tp_price, "floating_tp"
        peak_price = max(peak_price, high, close_now)
        if not armed and peak_price / entry - 1.0 >= arm_pct:
            armed = True
        if armed and trail_dd > 0:
            dd = 1.0 - close_now / peak_price if peak_price > 0 else 0.0
            if dd >= trail_dd:
                return entry, close_now, "floating_trail"
    return entry, float(trade_window.iloc[-1]["close"]), "close"


def _dynamic_decay_exit_price(
    hist: pd.DataFrame,
    entry_price: float,
    trade_cfg: TradeExecutionConfig,
    cross_eff: Optional[dict] = None,
) -> Tuple[Optional[float], str]:
    """
    动态持仓的日级离场判断。返回(离场价, 原因)；不离场返回(None, "hold")。
    """
    cross_eff = cross_eff or {}
    decay_both = int(cross_eff.get("decay_exit_require_ma_and_macd", 0)) == 1
    return dynamic_decay_exit_signal(
        hist=hist,
        entry_price=entry_price,
        tp1_pct=float(trade_cfg.tp1_pct),
        tp2_pct=float(trade_cfg.tp2_pct),
        decay_require_ma_and_macd=decay_both,
    )


def run_backtest_dynamic_decay(
    index_df: pd.DataFrame,
    universe: List[str],
    data: Dict[str, pd.DataFrame],
    tf: ThrottledFetcher,
    risk_cfg: RiskControlConfig,
    trade_cfg: TradeExecutionConfig,
    top_n: int,
    hold_days: int,
    max_hold_days: int,
    strategy_mode: str,
    meta: pd.DataFrame,
    concept_meta: pd.DataFrame,
    cross_flow_base: dict,
    regime_cf_overrides: Dict[str, Dict],
    raw_risk_dict: Optional[dict] = None,
    raw_trade_dict: Optional[dict] = None,
) -> pd.DataFrame:
    """
    动态持仓回测：
    - 每日可入场（非固定N天选一次）；
    - 每个交易日检查离场，允许超过 hold_days 持有；
    - max_hold_days 为最长持有上限。
    """
    dates = list(index_df.index.sort_values().unique())
    positions: Dict[str, dict] = {}
    nav = 1.0
    benchmark_nav = 1.0
    records = []
    trade_records = []
    signal_records = []
    stoploss_hits = 0
    tp1_hits = 0
    tp2_hits = 0
    decay_hits = 0
    maxhold_hits = 0

    raw_risk_dict = raw_risk_dict or {}
    raw_trade_dict = raw_trade_dict or {}

    for i in range(1, len(dates)):
        d_prev = dates[i - 1]
        d_now = dates[i]

        # regime/cross 参数按当前日更新
        idx_hist = index_df[index_df.index <= d_now].tail(200)
        if len(idx_hist) < 80:
            continue
        regime = get_regime_asof(idx_hist)
        cross_eff = merge_cross_flow_for_regime(cross_flow_base, regime, regime_cf_overrides)
        # v0.5: 双引擎自动切换。bull 用主升浪 hybrid; range/bear 用盘整启动 v4_pure
        # 进而切换 trade_cfg 的止损止盈与 max_hold_days, risk_cfg 的 box/runup/accum 阈值
        active_mode_today = resolve_scoring_mode(strategy_mode, regime)
        eff_trade_cfg = effective_trade_cfg(trade_cfg, active_mode_today, raw_trade_dict)
        eff_risk_cfg = effective_risk_cfg(risk_cfg, active_mode_today, raw_risk_dict)
        eff_max_hold = effective_max_hold_days(max_hold_days, active_mode_today, raw_trade_dict)

        # 先做持仓日收益与离场判定
        day_rets = []
        for code in list(positions.keys()):
            pos = positions[code]
            sdf = data.get(code, pd.DataFrame())
            if sdf.empty or d_now not in sdf.index:
                continue
            hist = sdf[sdf.index <= d_now]
            prev_mark = float(pos["mark_price"])
            exit_price, exit_reason = _dynamic_decay_exit_price(hist, float(pos["entry_price"]), eff_trade_cfg, cross_eff=cross_eff)
            mark_price = float(hist.iloc[-1]["close"])
            if exit_price is not None and exit_reason != "hold":
                pnl = exit_price / prev_mark - 1 if prev_mark > 0 else 0.0
                day_rets.append(pnl)
                gross_ret = float(exit_price / float(pos["entry_price"]) - 1) if float(pos["entry_price"]) > 0 else np.nan
                hold_days_real = int(pos.get("bars_held", 0)) + 1
                trade_records.append(
                    {
                        "entry_date": pd.Timestamp(pos["entry_date"]).strftime("%Y-%m-%d"),
                        "exit_date": d_now.strftime("%Y-%m-%d"),
                        "ts_code": code,
                        "entry_price": float(pos["entry_price"]),
                        "exit_price": float(exit_price),
                        "gross_return": gross_ret,
                        "hold_days": hold_days_real,
                        "exit_reason": exit_reason,
                        "regime_exit": regime,
                        "active_mode_entry": pos.get("active_mode", "hybrid"),
                        "active_mode_exit": active_mode_today,
                    }
                )
                signal_records.append(
                    {
                        "date": d_now.strftime("%Y-%m-%d"),
                        "action": "exit",
                        "ts_code": code,
                        "exit_price": float(exit_price),
                        "regime": regime,
                        "active_mode": active_mode_today,
                        "exit_reason": exit_reason,
                    }
                )
                del positions[code]
                if exit_reason == "tp1":
                    tp1_hits += 1
                elif exit_reason == "tp2":
                    tp2_hits += 1
                elif exit_reason == "decay":
                    decay_hits += 1
            else:
                pnl = mark_price / prev_mark - 1 if prev_mark > 0 else 0.0
                day_rets.append(pnl)
                pos["mark_price"] = mark_price
                pos["bars_held"] = int(pos["bars_held"]) + 1
                if pos["bars_held"] >= eff_max_hold:
                    gross_ret = float(mark_price / float(pos["entry_price"]) - 1) if float(pos["entry_price"]) > 0 else np.nan
                    hold_days_real = int(pos.get("bars_held", 0))
                    trade_records.append(
                        {
                            "entry_date": pd.Timestamp(pos["entry_date"]).strftime("%Y-%m-%d"),
                            "exit_date": d_now.strftime("%Y-%m-%d"),
                            "ts_code": code,
                            "entry_price": float(pos["entry_price"]),
                            "exit_price": mark_price,
                            "gross_return": gross_ret,
                            "hold_days": hold_days_real,
                            "exit_reason": "max_hold",
                            "regime_exit": regime,
                            "active_mode_entry": pos.get("active_mode", "hybrid"),
                            "active_mode_exit": active_mode_today,
                        }
                    )
                    signal_records.append(
                        {
                            "date": d_now.strftime("%Y-%m-%d"),
                            "action": "exit",
                            "ts_code": code,
                            "exit_price": mark_price,
                            "regime": regime,
                            "active_mode": active_mode_today,
                            "exit_reason": "max_hold",
                        }
                    )
                    del positions[code]
                    maxhold_hits += 1

        port_ret = float(np.mean(day_rets)) if day_rets else 0.0
        nav *= 1 + port_ret

        # 基准按日收益
        bench_ret = 0.0
        if d_prev in index_df.index and d_now in index_df.index:
            c0 = float(index_df.loc[d_prev, "close"])
            c1 = float(index_df.loc[d_now, "close"])
            if c0 > 0:
                bench_ret = c1 / c0 - 1
        benchmark_nav *= 1 + bench_ret

        # 每日可补仓到 top_n
        if len(positions) < top_n:
            # 动态日级回测中避免每天调用 daily_basic（速度过慢），改为不依赖当日换手率筛选。
            daily_basic = pd.DataFrame()
            active_mode = active_mode_today
            ranked = score_universe_asof(d_now, universe, data, daily_basic, regime, strategy_mode=active_mode)
            if "ts_code" in ranked.columns:
                ranked = ranked.set_index("ts_code", drop=False)
            if not daily_basic.empty:
                db = daily_basic[["ts_code", "turnover_rate"]].drop_duplicates(subset=["ts_code"]).set_index("ts_code")
                ranked = ranked.join(db, how="left")
            else:
                ranked["turnover_rate"] = np.nan
            ranked = ranked.join(meta, how="left")
            ranked = ranked.join(concept_meta, how="left")

            ranked_before_rotation = ranked.copy()
            if "concept_name" in ranked.columns and "momentum_20d" in ranked.columns:
                concept_strength = (
                    ranked.groupby("concept_name", dropna=False)["momentum_20d"].mean().reset_index(name="ret_20d_mean")
                )
                if not concept_strength.empty and concept_strength["ret_20d_mean"].notna().any():
                    mn = concept_strength["ret_20d_mean"].min()
                    mx = concept_strength["ret_20d_mean"].max()
                    if pd.notna(mn) and pd.notna(mx) and abs(mx - mn) > 1e-12:
                        concept_strength["concept_rotation_score"] = (
                            (concept_strength["ret_20d_mean"] - mn) / (mx - mn) * 100
                        ).clip(0, 100)
                    else:
                        concept_strength["concept_rotation_score"] = 50.0
                else:
                    concept_strength["concept_rotation_score"] = 50.0
                rot_map = concept_strength.set_index("concept_name")["concept_rotation_score"].to_dict()
                ranked["concept_rotation_score"] = ranked["concept_name"].map(rot_map)
                rot_min = rotation_floor_from_cross(cross_eff, regime)
                if int(cross_eff.get("rotation_pool_hard_gate", 0)) == 1:
                    ranked_rot = ranked[
                        ranked["concept_rotation_score"].isna()
                        | (ranked["concept_rotation_score"] >= rot_min)
                    ]
                    if len(ranked_rot) >= max(top_n, 10):
                        ranked = ranked_rot
                    else:
                        ranked = ranked_before_rotation

            # v4_pure 时给 total_score 注入板块轮动加权(v0.5 实盘 0.10~0.15 权重的近似)
            if active_mode == "v4_pure" and "concept_rotation_score" in ranked.columns and "total_score" in ranked.columns:
                rot = pd.to_numeric(ranked["concept_rotation_score"], errors="coerce").fillna(50.0)
                ranked["total_score"] = ranked["total_score"] * 0.85 + rot * 0.15
                ranked = ranked.sort_values("total_score", ascending=False)

            filtered = apply_risk_filters(ranked, eff_risk_cfg, active_mode=active_mode)
            if len(filtered) < top_n:
                filtered = ranked.copy()
            picks = pick_with_industry_cap(filtered, top_n, eff_risk_cfg.max_industry_ratio)
            if not picks:
                picks = [c for c in ranked.index.tolist() if c in data][: max(1, top_n - len(positions))]
            scored = [
                (
                    c,
                    entry_signal_score(
                        data[c],
                        d_now,
                        eff_trade_cfg,
                        regime=regime,
                        cross_eff=cross_eff,
                        active_mode=active_mode,
                        risk_cfg=eff_risk_cfg,
                        concept_rotation_score=concept_rotation_score_for_code(ranked, c),
                    ),
                )
                for c in picks
                if c in data
            ]
            pref_min = entry_score_pref_min_from_cross(cross_eff, regime)
            picks = [c for c, s in scored if s >= pref_min]
            if not picks:
                picks = [c for c, _ in scored[: max(1, top_n - len(positions))]]

            engine_usage_local = active_mode  # for record
            for c in picks:
                if c in positions:
                    continue
                sdf = data.get(c, pd.DataFrame())
                if sdf.empty or d_now not in sdf.index:
                    continue
                entry_price = float(sdf.loc[d_now, "close"])
                if entry_price <= 0:
                    continue
                positions[c] = {
                    "entry_date": d_now,
                    "entry_price": entry_price,
                    "mark_price": entry_price,
                    "bars_held": 0,
                    "active_mode": engine_usage_local,
                }
                signal_records.append(
                    {
                        "date": d_now.strftime("%Y-%m-%d"),
                        "action": "entry",
                        "ts_code": c,
                        "entry_price": entry_price,
                        "regime": regime,
                        "active_mode": engine_usage_local,
                        "top_n": top_n,
                        "max_hold_days": eff_max_hold,
                        "entry_score_pref_min": pref_min,
                    }
                )
                if len(positions) >= top_n:
                    break

        records.append(
            {
                "date": d_now.strftime("%Y-%m-%d"),
                "regime": regime,
                "active_mode": active_mode_today,
                "hold_count": len(positions),
                "portfolio_ret": port_ret,
                "benchmark_ret": bench_ret,
                "excess_ret": port_ret - bench_ret,
                "nav": nav,
                "benchmark_nav": benchmark_nav,
            }
        )

    bt = pd.DataFrame(records)
    if bt.empty:
        raise ValueError("动态持仓回测结果为空。")
    total_periods = len(bt)
    strategy_win_rate = float((bt["portfolio_ret"] > 0).mean())
    excess_win_rate = float((bt["excess_ret"] > 0).mean())
    periods_per_year = 252
    annual_ret = float(bt["nav"].iloc[-1] ** (periods_per_year / total_periods) - 1)
    annual_bench = float(bt["benchmark_nav"].iloc[-1] ** (periods_per_year / total_periods) - 1)
    std = bt["portfolio_ret"].std()
    sharpe = float((bt["portfolio_ret"].mean() / std) * np.sqrt(periods_per_year)) if std > 0 else np.nan
    dd = bt["nav"] / bt["nav"].cummax() - 1
    max_dd = float(dd.min())

    engine_usage_summary: Dict[str, int] = {"hybrid": 0, "v4_pure": 0}
    if "active_mode" in bt.columns:
        for k, v in bt["active_mode"].value_counts().to_dict().items():
            engine_usage_summary[str(k)] = int(v)

    summary = {
        "periods": total_periods,
        "strategy_win_rate": strategy_win_rate,
        "excess_win_rate": excess_win_rate,
        "annual_return": annual_ret,
        "benchmark_annual_return": annual_bench,
        "max_drawdown": max_dd,
        "sharpe_annualized": None if pd.isna(sharpe) else sharpe,
        "stoploss_hit_total": stoploss_hits,
        "tp1_hit_total": tp1_hits,
        "tp2_hit_total": tp2_hits,
        "decay_hit_total": decay_hits,
        "maxhold_hit_total": maxhold_hits,
        "dynamic_mode": True,
        "strategy_mode": strategy_mode,
        "engine_usage_days": engine_usage_summary,
    }
    bt.attrs["summary"] = summary
    bt.attrs["trade_records"] = trade_records
    bt.attrs["signal_records"] = signal_records
    return bt


def run_backtest(
    token: str,
    years: int = 5,
    top_n: int = 8,
    rebalance_freq: str = "M",
    hold_days: int = 5,
    execution_mode: str = "systematic",
    cache_dir: str = "backtest_cache/daily",
    risk_cfg: RiskControlConfig = None,
    trade_cfg: TradeExecutionConfig = None,
    save_output: bool = True,
    regime_cross_flow: bool = True,
    cross_flow_regimes_path: str = "config/cross_flow_regimes.json",
    regime_floating_pnl: bool = True,
    floating_pnl_regimes_path: str = "config/floating_pnl_regimes.json",
    max_hold_days: int = 30,
) -> pd.DataFrame:
    if years < 1 or years > 10:
        raise ValueError("years 请设置在 1~10 之间。")
    if rebalance_freq not in ("M", "D"):
        raise ValueError("rebalance_freq 仅支持 'M' 或 'D'。")
    if hold_days < 1 or hold_days > 30:
        raise ValueError("hold_days 请设置在 1~30。")
    if execution_mode not in (
        "systematic",
        "discretionary",
        "discretionary_decay",
        "discretionary_context",
        "floating_pnl",
        "dynamic_decay",
    ):
        raise ValueError(
            "execution_mode 仅支持 'systematic'、'discretionary'、'discretionary_decay'、'discretionary_context'、'floating_pnl' 或 'dynamic_decay'。"
        )

    tf = ThrottledFetcher(token, ThrottleConfig())
    risk_cfg = risk_cfg or load_backtest_risk_config()
    trade_cfg = trade_cfg or load_trade_execution_config()
    cross_flow_base = load_cross_flow_config()
    regime_cf_overrides = load_regime_cross_flow_overrides(cross_flow_regimes_path) if regime_cross_flow else {}
    regime_floating_overrides = (
        load_floating_pnl_regime_overrides(floating_pnl_regimes_path) if regime_floating_pnl else {}
    )
    strategy_mode = load_strategy_config().get("mode", "hybrid")
    raw_risk_dict = load_searchv1_risk_dict()
    raw_trade_dict = load_searchv1_trade_dict()
    print(
        f"策略模式: {strategy_mode} | v0.5 dual_engine 自动切换("
        f"bull→主升浪hybrid, range/bear→盘整启动v4_pure)"
    )
    engine_usage = {"hybrid": 0, "v4_pure": 0}
    today = datetime.now().date()
    raw_end = today.strftime("%Y-%m-%d")
    end_date = resolve_backtest_end_date(tf, raw_end, lookback_days=10)
    start_date = (datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=365 * years + 260)).strftime(
        "%Y-%m-%d"
    )

    print(f"回测区间: {start_date} ~ {end_date}")
    print("正在获取指数与股票池...")
    index_df = tf.get_index_daily("000001.SH", start_date, end_date)
    theme_cfg = load_theme_config()
    theme_keywords = theme_cfg["keywords"]
    pool = build_candidate_pool(
        tf.fetcher,
        theme_keywords,
        alias_map=theme_cfg.get("aliases"),
        fuzzy_cutoff=theme_cfg.get("fuzzy_cutoff", 0.55),
        fuzzy_top_n=theme_cfg.get("fuzzy_top_n", 2),
        extra_ths_indices=theme_cfg.get("extra_ths_indices", []),
    )
    meta = pool[["ts_code", "industry"]].drop_duplicates(subset=["ts_code"]).set_index("ts_code")
    concept_meta = pool[["ts_code", "concept_name"]].drop_duplicates(subset=["ts_code"]).set_index("ts_code")
    concept_members_map = (
        pool[["concept_name", "ts_code"]]
        .dropna(subset=["concept_name"])
        .groupby("concept_name")["ts_code"]
        .apply(lambda s: [str(x) for x in s.drop_duplicates().tolist()])
        .to_dict()
    )
    universe = pool["ts_code"].drop_duplicates().tolist()
    print(f"初始股票池数量: {len(universe)}")

    print("正在拉取并缓存个股历史数据（首次会较慢）...")
    data = {}
    cache_path = Path(cache_dir)
    for i, code in enumerate(universe, 1):
        df = load_or_fetch_daily(tf, code, start_date, end_date, cache_path)
        if len(df) >= 120:
            data[code] = df.sort_index()
        if i % 100 == 0:
            print(f"  进度: {i}/{len(universe)}")

    universe = list(data.keys())
    print(f"可用股票池数量: {len(universe)}")
    if len(universe) < top_n:
        raise ValueError("可用股票数不足，请缩短回测年限或降低 top_n。")

    if execution_mode == "dynamic_decay":
        bt = run_backtest_dynamic_decay(
            index_df=index_df,
            universe=universe,
            data=data,
            tf=tf,
            risk_cfg=risk_cfg,
            trade_cfg=trade_cfg,
            top_n=top_n,
            hold_days=hold_days,
            max_hold_days=max_hold_days,
            strategy_mode=strategy_mode,
            meta=meta,
            concept_meta=concept_meta,
            cross_flow_base=cross_flow_base,
            regime_cf_overrides=regime_cf_overrides,
            raw_risk_dict=raw_risk_dict,
            raw_trade_dict=raw_trade_dict,
        )
        s = bt.attrs.get("summary", {})
        print("\n==== 回测结果摘要 ====")
        print(f"调仓期数: {int(s.get('periods', 0))}")
        print(f"策略胜率(单期收益>0): {float(s.get('strategy_win_rate', 0)):.2%}")
        print(f"超额胜率(跑赢指数): {float(s.get('excess_win_rate', 0)):.2%}")
        print(f"策略年化: {float(s.get('annual_return', 0)):.2%}")
        print(f"基准年化: {float(s.get('benchmark_annual_return', 0)):.2%}")
        shp = s.get("sharpe_annualized")
        print(f"夏普(年化): {shp:.2f}" if shp is not None else "夏普(年化): N/A")
        print(f"最大回撤: {float(s.get('max_drawdown', 0)):.2%}")
        print(f"TP1触发总次数: {int(s.get('tp1_hit_total', 0))}")
        print(f"TP2触发总次数: {int(s.get('tp2_hit_total', 0))}")
        print(f"衰减离场总次数: {int(s.get('decay_hit_total', 0))}")
        print(f"最长持有离场总次数: {int(s.get('maxhold_hit_total', 0))}")
        if save_output:
            out_dir = Path("output") / "backtest"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            detail_file = out_dir / f"backtest_detail_{years}y_{stamp}.csv"
            summary_file = out_dir / f"backtest_summary_{years}y_{stamp}.json"
            trades_file = out_dir / f"backtest_trades_{years}y_{stamp}.csv"
            signals_file = out_dir / f"backtest_signals_{years}y_{stamp}.csv"
            bt.to_csv(detail_file, index=False, encoding="utf-8-sig")
            pd.DataFrame(bt.attrs.get("trade_records", [])).to_csv(trades_file, index=False, encoding="utf-8-sig")
            pd.DataFrame(bt.attrs.get("signal_records", [])).to_csv(signals_file, index=False, encoding="utf-8-sig")
            summary_payload = {
                "years": years,
                "top_n": top_n,
                "rebalance_freq": rebalance_freq,
                "hold_days": hold_days,
                "max_hold_days": max_hold_days,
                "execution_mode": execution_mode,
                "trades_file": str(trades_file),
                "signals_file": str(signals_file),
                **s,
            }
            summary_file.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n明细输出: {detail_file}")
            print(f"摘要输出: {summary_file}")
            print(f"交易明细输出: {trades_file}")
            print(f"信号明细输出: {signals_file}")
        return bt

    if rebalance_freq == "M":
        rebalance_dates = month_end_dates(index_df)
    else:
        rebalance_dates = rolling_rebalance_dates(index_df, hold_days=hold_days)
    rebalance_dates = [d for d in rebalance_dates if d >= pd.Timestamp(start_date) and d <= pd.Timestamp(end_date)]
    if len(rebalance_dates) < 3:
        raise ValueError("可用调仓日不足。")

    records = []
    portfolio_curve = []
    nav = 1.0
    benchmark_nav = 1.0

    # 第一个月只用于建仓，不统计收益
    for i in range(len(rebalance_dates) - 1):
        rebalance_date = rebalance_dates[i]
        next_date = rebalance_dates[i + 1]

        idx_hist = index_df[index_df.index <= rebalance_date].tail(200)
        if len(idx_hist) < 80:
            continue
        regime = get_regime_asof(idx_hist)
        cross_eff = merge_cross_flow_for_regime(cross_flow_base, regime, regime_cf_overrides)
        floating_eff = merge_floating_cfg_for_regime(trade_cfg, regime, regime_floating_overrides)

        daily_basic = tf.get_daily_basic(universe, rebalance_date.strftime("%Y-%m-%d"))
        active_mode = resolve_scoring_mode(strategy_mode, regime)
        engine_usage[active_mode] = engine_usage.get(active_mode, 0) + 1
        # v0.5: 双引擎模式下按 active_mode 切换 trade/risk 阈值
        eff_trade_cfg = effective_trade_cfg(trade_cfg, active_mode, raw_trade_dict)
        eff_risk_cfg = effective_risk_cfg(risk_cfg, active_mode, raw_risk_dict)
        ranked = score_universe_asof(
            rebalance_date,
            universe,
            data,
            daily_basic,
            regime,
            strategy_mode=active_mode,
        )
        if "ts_code" in ranked.columns:
            ranked = ranked.set_index("ts_code", drop=False)
        # 仅保留在调仓日已具备足够历史、且下一周期可交易的股票
        tradable_codes = []
        for code in ranked.index.tolist():
            sdf = data.get(code, pd.DataFrame())
            if sdf.empty:
                continue
            hist_len = int((sdf.index <= rebalance_date).sum())
            has_future_bar = bool((sdf.index > rebalance_date).any())
            if hist_len >= 80 and has_future_bar:
                tradable_codes.append(code)
        if tradable_codes:
            ranked = ranked[ranked.index.isin(tradable_codes)].copy()
        if not daily_basic.empty:
            db = daily_basic[["ts_code", "turnover_rate"]].drop_duplicates(subset=["ts_code"]).set_index("ts_code")
            ranked = ranked.join(db, how="left")
        else:
            ranked["turnover_rate"] = np.nan
        ranked = ranked.join(meta, how="left")
        ranked = ranked.join(concept_meta, how="left")
        ranked_before_rotation = ranked.copy()
        # 板块轮动过滤：按概念内20日动量均值做横截面评分
        if "concept_name" in ranked.columns and "momentum_20d" in ranked.columns:
            concept_strength = (
                ranked.groupby("concept_name", dropna=False)["momentum_20d"].mean().reset_index(name="ret_20d_mean")
            )
            if not concept_strength.empty and concept_strength["ret_20d_mean"].notna().any():
                mn = concept_strength["ret_20d_mean"].min()
                mx = concept_strength["ret_20d_mean"].max()
                if pd.notna(mn) and pd.notna(mx) and abs(mx - mn) > 1e-12:
                    concept_strength["concept_rotation_score"] = (
                        (concept_strength["ret_20d_mean"] - mn) / (mx - mn) * 100
                    ).clip(0, 100)
                else:
                    concept_strength["concept_rotation_score"] = 50.0
            else:
                concept_strength["concept_rotation_score"] = 50.0
            rot_map = concept_strength.set_index("concept_name")["concept_rotation_score"].to_dict()
            ranked["concept_rotation_score"] = ranked["concept_name"].map(rot_map)
            rot_min = rotation_floor_from_cross(cross_eff, regime)
            if int(cross_eff.get("rotation_pool_hard_gate", 0)) == 1:
                ranked_rot = ranked[
                    ranked["concept_rotation_score"].isna()
                    | (ranked["concept_rotation_score"] >= rot_min)
                ]
                if len(ranked_rot) >= max(top_n, 10):
                    ranked = ranked_rot
                else:
                    ranked = ranked_before_rotation

        # v4_pure 时给 total_score 注入板块轮动加权(对齐 v0.5 0.10~0.15 权重)
        if active_mode == "v4_pure" and "concept_rotation_score" in ranked.columns and "total_score" in ranked.columns:
            rot = pd.to_numeric(ranked["concept_rotation_score"], errors="coerce").fillna(50.0)
            ranked["total_score"] = ranked["total_score"] * 0.85 + rot * 0.15
            ranked = ranked.sort_values("total_score", ascending=False)

        filtered = apply_risk_filters(ranked, eff_risk_cfg, active_mode=active_mode)
        if len(filtered) < top_n:
            # 若过滤后数量不足，则放宽到“只做行业约束”，防止空仓过多
            filtered = ranked.copy()
        picks = pick_with_industry_cap(filtered, top_n, eff_risk_cfg.max_industry_ratio)
        scored_picks = [
            (
                c,
                entry_signal_score(
                    data[c],
                    rebalance_date,
                    eff_trade_cfg,
                    regime=regime,
                    cross_eff=cross_eff,
                    active_mode=active_mode,
                    risk_cfg=eff_risk_cfg,
                    concept_rotation_score=concept_rotation_score_for_code(ranked, c),
                ),
            )
            for c in picks
            if c in data
        ]
        pref_min = entry_score_pref_min_from_cross(cross_eff, regime)
        # v0.6 修复: 原逻辑 bear `pref[:1]`、bull/range 不补齐, 导致每期实际只持 1 只。
        # 改为「以偏好为种子, 按 total_score 顺序补足到 regime 目标」, 保留偏好优先, 同时降低单股方差。
        cap = max(1, min(top_n, eff_trade_cfg.max_daily_picks))
        regime_target_ratio = {"bull": 1.0, "range": 0.75, "bear": 0.50}.get(regime, 1.0)
        desired = max(2, int(round(cap * regime_target_ratio))) if cap >= 2 else 1
        if regime == "bear":
            bear_min = max(2, pref_min)
            pref = [c for c, s in scored_picks if s >= bear_min]
        else:
            pref = [c for c, s in scored_picks if s >= pref_min]
        seen: set = set(pref)
        picks = list(pref)
        for _c, _ in scored_picks:
            if len(picks) >= desired:
                break
            if _c not in seen:
                picks.append(_c)
                seen.add(_c)
        picks = picks[:desired]
        if len(picks) > eff_trade_cfg.max_daily_picks:
            picks = picks[: eff_trade_cfg.max_daily_picks]
        if not picks:
            fallback_codes = [c for c in ranked.index.tolist() if c in data]
            picks = fallback_codes[: max(1, min(2, cap))]
        if os.environ.get("FUNNEL_DEBUG") == "1":
            print(
                f"[funnel] {rebalance_date.strftime('%Y-%m-%d')} regime={regime} "
                f"ranked={len(ranked)} filtered={len(filtered)} "
                f"after_cap={len(scored_picks)} pref={len(pref)} "
                f"desired={desired} picks={len(picks)}",
                flush=True,
            )
        concept_by_code = ranked["concept_name"].to_dict() if "concept_name" in ranked.columns else {}

        rets = []
        stoploss_hits = 0
        context_exit_hits = 0
        trail_exit_hits = 0
        floating_stop_hits = 0
        floating_tp_hits = 0
        floating_trail_hits = 0
        tp1_hits = 0
        tp2_hits = 0
        for code in picks:
            sdf = data[code]
            if execution_mode == "discretionary":
                entry, exitp, exit_reason = period_exit_discretionary(
                    sdf,
                    rebalance_date=rebalance_date,
                    next_date=next_date,
                    trade_cfg=eff_trade_cfg,
                )
            elif execution_mode == "discretionary_decay":
                entry, exitp, exit_reason = period_exit_discretionary_decay(
                    sdf,
                    rebalance_date=rebalance_date,
                    next_date=next_date,
                    trade_cfg=eff_trade_cfg,
                    cross_eff=cross_eff if regime_cross_flow else None,
                )
            elif execution_mode == "discretionary_context":
                entry, exitp, exit_reason = period_exit_discretionary_context(
                    code=code,
                    df=sdf,
                    concept_name=str(concept_by_code.get(code, "") or ""),
                    concept_members_map=concept_members_map,
                    all_data=data,
                    rebalance_date=rebalance_date,
                    next_date=next_date,
                    trade_cfg=eff_trade_cfg,
                )
            elif execution_mode == "floating_pnl":
                entry, exitp, exit_reason = period_exit_floating_pnl(
                    sdf,
                    rebalance_date=rebalance_date,
                    next_date=next_date,
                    trade_cfg=eff_trade_cfg,
                    floating_eff=floating_eff if regime_floating_pnl else None,
                )
            else:
                entry, exitp, exit_reason = period_exit_with_trade_plan(
                    sdf,
                    rebalance_date=rebalance_date,
                    next_date=next_date,
                    trade_cfg=eff_trade_cfg,
                )
            if pd.notna(entry) and pd.notna(exitp) and entry > 0:
                rets.append(exitp / entry - 1)
                if exit_reason == "stop":
                    stoploss_hits += 1
                elif exit_reason == "context_exit":
                    context_exit_hits += 1
                elif exit_reason == "trail_exit":
                    trail_exit_hits += 1
                elif exit_reason == "floating_stop":
                    floating_stop_hits += 1
                elif exit_reason == "floating_tp":
                    floating_tp_hits += 1
                elif exit_reason == "floating_trail":
                    floating_trail_hits += 1
                elif exit_reason == "tp1":
                    tp1_hits += 1
                elif exit_reason == "tp2":
                    tp2_hits += 1

        if not rets:
            port_ret = 0.0
            win_rate = 0.0
        else:
            port_ret = float(np.mean(rets))
            win_rate = float(np.mean(np.array(rets) > 0))

        idx_entry = next_trade_price(index_df, rebalance_date)
        idx_exit = hold_to_date_price(index_df, next_date)
        bench_ret = 0.0
        if pd.notna(idx_entry) and pd.notna(idx_exit) and idx_entry > 0:
            bench_ret = float(idx_exit / idx_entry - 1)

        nav *= 1 + port_ret
        benchmark_nav *= 1 + bench_ret

        rec = {
            "rebalance_date": rebalance_date.strftime("%Y-%m-%d"),
            "next_date": next_date.strftime("%Y-%m-%d"),
            "regime": regime,
            "active_mode": active_mode,
            "pick_count": len(rets),
            "portfolio_ret": port_ret,
            "benchmark_ret": bench_ret,
            "excess_ret": port_ret - bench_ret,
            "period_win_rate": win_rate,
            "stoploss_hit_count": stoploss_hits,
            "context_exit_count": context_exit_hits,
            "trail_exit_count": trail_exit_hits,
            "floating_stop_count": floating_stop_hits,
            "floating_tp_count": floating_tp_hits,
            "floating_trail_count": floating_trail_hits,
            "tp1_hit_count": tp1_hits,
            "tp2_hit_count": tp2_hits,
            "nav": nav,
            "benchmark_nav": benchmark_nav,
        }
        records.append(rec)
        portfolio_curve.append(nav)

    if not records:
        raise ValueError("回测结果为空，请检查数据质量或参数。")

    bt = pd.DataFrame(records)
    total_periods = len(bt)
    strategy_win_rate = float((bt["portfolio_ret"] > 0).mean())
    excess_win_rate = float((bt["excess_ret"] > 0).mean())
    periods_per_year = 12 if rebalance_freq == "M" else max(1, int(252 / hold_days))
    annual_ret = float(bt["nav"].iloc[-1] ** (periods_per_year / total_periods) - 1)
    annual_bench = float(bt["benchmark_nav"].iloc[-1] ** (periods_per_year / total_periods) - 1)
    sharpe = float((bt["portfolio_ret"].mean() / bt["portfolio_ret"].std()) * np.sqrt(periods_per_year)) if bt[
        "portfolio_ret"
    ].std() > 0 else np.nan
    dd = bt["nav"] / bt["nav"].cummax() - 1
    max_dd = float(dd.min())

    print("\n==== 回测结果摘要 ====")
    print(f"调仓期数: {total_periods}")
    print(f"策略胜率(单期收益>0): {strategy_win_rate:.2%}")
    print(f"超额胜率(跑赢指数): {excess_win_rate:.2%}")
    print(f"策略年化: {annual_ret:.2%}")
    print(f"基准年化: {annual_bench:.2%}")
    print(f"夏普(年化): {sharpe:.2f}" if pd.notna(sharpe) else "夏普(年化): N/A")
    print(f"最大回撤: {max_dd:.2%}")
    print(f"止损触发总次数: {int(bt['stoploss_hit_count'].sum())}")
    print(f"Context触发总次数: {int(bt['context_exit_count'].sum())}")
    print(f"浮盈回撤离场总次数: {int(bt['trail_exit_count'].sum())}")
    print(f"Floating止损总次数: {int(bt['floating_stop_count'].sum())}")
    print(f"Floating止盈总次数: {int(bt['floating_tp_count'].sum())}")
    print(f"Floating移动保护总次数: {int(bt['floating_trail_count'].sum())}")
    print(f"TP1触发总次数: {int(bt['tp1_hit_count'].sum())}")
    print(f"TP2触发总次数: {int(bt['tp2_hit_count'].sum())}")

    summary = {
        "years": years,
        "top_n": top_n,
        "rebalance_freq": rebalance_freq,
        "hold_days": hold_days,
        "periods": total_periods,
        "strategy_win_rate": strategy_win_rate,
        "excess_win_rate": excess_win_rate,
        "annual_return": annual_ret,
        "benchmark_annual_return": annual_bench,
        "max_drawdown": max_dd,
        "sharpe_annualized": None if pd.isna(sharpe) else sharpe,
        "stoploss_hit_total": int(bt["stoploss_hit_count"].sum()),
        "context_exit_total": int(bt["context_exit_count"].sum()),
        "trail_exit_total": int(bt["trail_exit_count"].sum()),
        "floating_stop_total": int(bt["floating_stop_count"].sum()),
        "floating_tp_total": int(bt["floating_tp_count"].sum()),
        "floating_trail_total": int(bt["floating_trail_count"].sum()),
        "tp1_hit_total": int(bt["tp1_hit_count"].sum()),
        "tp2_hit_total": int(bt["tp2_hit_count"].sum()),
        "risk_control": {
            "max_volatility_30d": risk_cfg.max_volatility_30d,
            "min_breakout_readiness": risk_cfg.min_breakout_readiness,
            "turnover_rate_range": [risk_cfg.min_turnover_rate, risk_cfg.max_turnover_rate],
            "max_industry_ratio": risk_cfg.max_industry_ratio,
            "stop_loss_pct": risk_cfg.stop_loss_pct,
        },
        "trade_execution": {
            "execution_mode": execution_mode,
            "max_daily_picks": trade_cfg.max_daily_picks,
            "entry_breakout_buffer_pct": trade_cfg.entry_breakout_buffer_pct,
            "entry_need_above_ma5": trade_cfg.entry_need_above_ma5,
            "entry_min_ma5_slope_5d": trade_cfg.entry_min_ma5_slope_5d,
            "hard_stop_loss_pct": trade_cfg.hard_stop_loss_pct,
            "tp1_pct": trade_cfg.tp1_pct,
            "tp2_pct": trade_cfg.tp2_pct,
            "context_exit_require_both": trade_cfg.context_exit_require_both,
            "context_exit_only_when_profit": trade_cfg.context_exit_only_when_profit,
            "context_exit_min_hold_days": trade_cfg.context_exit_min_hold_days,
            "context_exit_confirm_days": trade_cfg.context_exit_confirm_days,
            "context_exit_sector_lookback_days": trade_cfg.context_exit_sector_lookback_days,
            "context_exit_sector_ret_mean_max": trade_cfg.context_exit_sector_ret_mean_max,
            "context_exit_sector_negative_ratio_min": trade_cfg.context_exit_sector_negative_ratio_min,
            "context_exit_leader_count_min": trade_cfg.context_exit_leader_count_min,
            "context_exit_leader_weak_ratio_min": trade_cfg.context_exit_leader_weak_ratio_min,
            "context_exit_leader_macd_slope_max": trade_cfg.context_exit_leader_macd_slope_max,
            "context_exit_min_profit_to_arm": trade_cfg.context_exit_min_profit_to_arm,
            "context_exit_trail_drawdown_pct": trade_cfg.context_exit_trail_drawdown_pct,
            "entry_rsi_death_veto_enabled": trade_cfg.entry_rsi_death_veto_enabled,
            "entry_rsi_death_line": trade_cfg.entry_rsi_death_line,
            "entry_rsi_period": trade_cfg.entry_rsi_period,
            "entry_rsi_death_veto_skip_pre_cross": trade_cfg.entry_rsi_death_veto_skip_pre_cross,
            "floating_stop_loss_pct": trade_cfg.floating_stop_loss_pct,
            "floating_take_profit_pct": trade_cfg.floating_take_profit_pct,
            "floating_min_profit_to_trail": trade_cfg.floating_min_profit_to_trail,
            "floating_trail_drawdown_pct": trade_cfg.floating_trail_drawdown_pct,
        },
        "strategy_mode": strategy_mode,
        "engine_usage": engine_usage,
        "regime_cross_flow": regime_cross_flow,
        "cross_flow_regimes_path": cross_flow_regimes_path if regime_cross_flow else None,
        "regime_floating_pnl": regime_floating_pnl,
        "floating_pnl_regimes_path": floating_pnl_regimes_path if regime_floating_pnl else None,
    }
    bt.attrs["summary"] = summary

    if save_output:
        out_dir = Path("output") / "backtest"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        detail_file = out_dir / f"backtest_detail_{years}y_{stamp}.csv"
        summary_file = out_dir / f"backtest_summary_{years}y_{stamp}.json"
        bt.to_csv(detail_file, index=False, encoding="utf-8-sig")
        summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细输出: {detail_file}")
        print(f"摘要输出: {summary_file}")
    return bt


def parse_args():
    p = argparse.ArgumentParser(description="沪深主板主题选股回测")
    p.add_argument("--years", type=int, default=5, help="回测年限，建议 5~10")
    p.add_argument("--top-n", type=int, default=8, help="每期持仓数量")
    p.add_argument("--rebalance-freq", type=str, default="D", help="调仓频率：D=按持仓天数滚动，M=月度")
    p.add_argument("--hold-days", type=int, default=5, help="持仓天数（D模式下生效），建议5")
    p.add_argument(
        "--execution-mode",
        type=str,
        default="systematic",
        help="执行模式：systematic/discretionary/discretionary_decay/discretionary_context/floating_pnl/dynamic_decay",
    )
    p.add_argument("--max-hold-days", type=int, default=30, help="dynamic_decay 模式下最长持有交易日")
    p.add_argument("--token", type=str, default="", help="可选：直接传 token；不传则读环境变量")
    p.add_argument("--risk-config", type=str, default="config/backtest_risk.json", help="回测风险参数配置文件")
    p.add_argument(
        "--regime-cross-flow",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否按牛/震荡/熊合并 cross_flow_regimes.json 覆盖（默认开启）",
    )
    p.add_argument(
        "--cross-flow-regimes",
        type=str,
        default="config/cross_flow_regimes.json",
        help="分状态 cross_flow 覆盖配置文件路径",
    )
    p.add_argument(
        "--regime-floating-pnl",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="floating_pnl 是否按 bull/range/bear 使用分状态参数（默认开启）",
    )
    p.add_argument(
        "--floating-pnl-regimes",
        type=str,
        default="config/floating_pnl_regimes.json",
        help="floating_pnl 分状态参数配置路径",
    )
    p.add_argument(
        "--profile",
        type=str,
        default="",
        choices=["", "mainline", "aggressive"],
        help="运行档位：mainline/aggressive（从 profile-config 读取参数覆盖）",
    )
    p.add_argument(
        "--profile-config",
        type=str,
        default="config/backtest_dynamic_decay_frozen.json",
        help="档位配置文件路径（默认读取冻结参数文件）",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    token = (args.token or os.getenv("TUSHARE_TOKEN", "")).strip()
    if not token:
        raise ValueError("请设置 TUSHARE_TOKEN 或通过 --token 传入。")
    cli_params = {
        "years": args.years,
        "top_n": args.top_n,
        "rebalance_freq": args.rebalance_freq,
        "hold_days": args.hold_days,
        "execution_mode": args.execution_mode,
        "max_hold_days": args.max_hold_days,
        "regime_cross_flow": args.regime_cross_flow,
    }
    if args.profile:
        prof = load_dynamic_decay_profile(args.profile, args.profile_config)
        for k in list(cli_params.keys()):
            if k in prof:
                cli_params[k] = prof[k]
        print(
            f"应用 profile={args.profile}: "
            f"years={cli_params['years']}, top_n={cli_params['top_n']}, "
            f"rebalance_freq={cli_params['rebalance_freq']}, hold_days={cli_params['hold_days']}, "
            f"execution_mode={cli_params['execution_mode']}, max_hold_days={cli_params['max_hold_days']}, "
            f"regime_cross_flow={cli_params['regime_cross_flow']}"
        )
    risk_cfg = load_backtest_risk_config(args.risk_config)
    run_backtest(
        token=token,
        years=int(cli_params["years"]),
        top_n=int(cli_params["top_n"]),
        rebalance_freq=str(cli_params["rebalance_freq"]),
        hold_days=int(cli_params["hold_days"]),
        execution_mode=str(cli_params["execution_mode"]),
        risk_cfg=risk_cfg,
        regime_cross_flow=bool(cli_params["regime_cross_flow"]),
        cross_flow_regimes_path=args.cross_flow_regimes,
        regime_floating_pnl=args.regime_floating_pnl,
        floating_pnl_regimes_path=args.floating_pnl_regimes,
        max_hold_days=int(cli_params["max_hold_days"]),
    )
