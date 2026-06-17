"""
沪深主板量化选股（十五五主题 + 蓄势上涨）
1) 自动过滤创业板/科创板
2) 限定十五五主题股票池
3) 横截面标准化打分，避免单股50分退化
"""

import json
import os
import re
import sys
import difflib
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

import numpy as np
import pandas as pd

from quant_data import DAILY_BASIC_FIELDS

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "box_range_monitor", "src"))
try:
    from box_range_monitor.signals import detect_box_signal
except ImportError:
    detect_box_signal = None

from execution_policy import dynamic_decay_levels, dynamic_decay_plan_texts

from lh_quant_app import (
    DEFAULT_CROSS_FLOW_CONFIG,
    DEFAULT_RISK_CONFIG,
    DEFAULT_STRATEGY_CONFIG,
    DEFAULT_THEME_KEYWORDS,
    DEFAULT_TRADE_CONFIG,
    DEFAULT_VALUATION_CONFIG,
    OUTPUT_COL_CN_MAP,
    apply_mode_overrides,
    load_cross_flow_config,
    load_risk_config,
    load_strategy_config,
    load_theme_config,
    load_trade_config,
    load_valuation_config,
    resolve_scoring_mode,
    to_chinese_columns,
)

DEFAULT_THEME_KEYWORDS = [
    "新型电力系统", "储能", "电网", "特高压",  # 能源
    "工业母机", "工业机器人", "智能制造",     # 制造
    "AIGC", "人工智能", "算力", "半导体",     # 数字/科技
    "CPO", "PCB", "光通信", "光模块",         # 算力硬件
    "低空经济", "无人机", "商业航天",         # 新质生产力
    "新材料概念", "合成生物", "生物制造",     # 前沿技术
    "创新药", "医疗器械概念",                 # 健康
    "物理AI", "算力网",                       # AI 基础设施
    "长鑫存储", "长江存储",                   # 国产存储
]


def load_theme_keywords(config_path: str = "config/themes.json") -> list[str]:
    cfg = load_theme_config(config_path)
    return cfg["keywords"]


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
        """
        返回不晚于 end_date 的最近交易日（上交所日历）。
        """
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
        basic = self.pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,name,area,industry,market",
        )
        basic = basic[basic["ts_code"].map(self.is_mainboard)].copy()
        basic = basic[~basic["name"].str.contains("ST", na=False)]
        return basic

    def _ths_index_theme_pool(
        self,
        keywords: list[str],
        alias_map: Optional[dict[str, list[str]]] = None,
    ) -> pd.DataFrame:
        """
        同花顺特色指数（ths_index + ths_member）。
        部分板块仅在同花顺指数中存在（如「先进封装」886009.TI），不在 concept(src='ts') 表内。
        """
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

    def _ths_explicit_indices_members(
        self,
        entries: Optional[list[dict[str, str]]],
    ) -> pd.DataFrame:
        """
        配置显式指定的同花顺指数 ts_code（不依赖名称匹配），ths_member 拉成分。
        """
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
        """
        用概念板块关键词构建主题池。
        - alias_map: themes.json 中的别名配置, 用于补充 direct 命中失败的情况。
        - fuzzy_cutoff/top_n: difflib 模糊匹配的阈值与候选数, 默认从配置注入,
          cutoff 默认 0.55(原 0.35 容易误匹), top_n 默认 2(原 3 易引噪)。
        - 合并同花顺 ths_index/ths_member：收录 concept(src='ts') 未覆盖的指数板块（如先进封装）。
        - extra_ths_indices：themes.json 显式给出的指数 ts_code + label，不经名称匹配直接并入成分。

        注意：概念归属会变化，建议每周人工复核一次关键词。
        """
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
        selected = {}
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
                fuzzy = difflib.get_close_matches(
                    kw, concept_names, n=int(fuzzy_top_n), cutoff=float(fuzzy_cutoff)
                )
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
        df = self.pro.daily(
            ts_code=ts_code,
            start_date=self._to_ts_date(start_date),
            end_date=self._to_ts_date(end_date),
        )
        if df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date")
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date")
        df["volume"] = df["vol"]
        df["pct_change"] = df["pct_chg"]
        df["amount"] = pd.to_numeric(df.get("amount", np.nan), errors="coerce")
        # 为 detect_box_signal 保留原始列并添加日期列
        df["date"] = df.index
        return df[["open", "high", "low", "close", "volume", "amount", "pct_change", "vol", "date"]]

    def get_daily_basic(self, ts_codes: list[str], trade_date: str) -> pd.DataFrame:
        ts_trade_date = self._to_ts_date(trade_date)
        df = self.pro.daily_basic(
            trade_date=ts_trade_date,
            fields=DAILY_BASIC_FIELDS,
        )
        if df.empty:
            return pd.DataFrame(columns=["ts_code", "total_mv", "circ_mv", "pb", "pe_ttm", "turnover_rate"])
        return df[df["ts_code"].isin(ts_codes)].drop_duplicates(subset=["ts_code"])

    def get_daily_basic_history(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        历史估值序列（PE_TTM/PB），带本地缓存，减少重复请求。
        """
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
            # 确保必需列存在
            for _req_col in ("pe_ttm", "pb"):
                if _req_col not in hist.columns:
                    hist[_req_col] = float("nan")
            hist = hist.sort_values("trade_date")
            hist.to_csv(cache_file, index=False, encoding="utf-8-sig")

        s = self._to_ts_date(start_date)
        e = self._to_ts_date(end_date)
        return hist[(hist["trade_date"] >= s) & (hist["trade_date"] <= e)].copy()

    def get_moneyflow_dc(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        个股资金流向（东财版）:
        接口名: moneyflow_dc
        """
        try:
            df = self.pro.moneyflow_dc(
                ts_code=ts_code,
                start_date=self._to_ts_date(start_date),
                end_date=self._to_ts_date(end_date),
            )
        except Exception:
            return pd.DataFrame()
        if df.empty:
            return df
        if "trade_date" in df.columns:
            df = df.sort_values("trade_date")
        return df

    def get_top_inst_by_dates(self, trade_dates: list[str]) -> pd.DataFrame:
        """
        龙虎榜机构交易:
        接口名: top_inst
        注意: 需逐日传 trade_date。
        """
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

    def get_share_float(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        限售股解禁:
        接口名: share_float
        """
        try:
            return self.pro.share_float(
                ts_code=ts_code,
                start_date=self._to_ts_date(start_date),
                end_date=self._to_ts_date(end_date),
            )
        except Exception:
            return pd.DataFrame()

    def get_stk_holdertrade(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        股东增减持:
        接口名: stk_holdertrade
        """
        try:
            return self.pro.stk_holdertrade(
                ts_code=ts_code,
                start_date=self._to_ts_date(start_date),
                end_date=self._to_ts_date(end_date),
            )
        except Exception:
            return pd.DataFrame()

    def get_fina_indicator_latest(self, ts_code: str, fields: str = "") -> pd.DataFrame:
        """
        财务指标:
        接口名: fina_indicator
        """
        try:
            if fields:
                df = self.pro.fina_indicator(ts_code=ts_code, fields=fields)
            else:
                df = self.pro.fina_indicator(ts_code=ts_code)
        except Exception:
            return pd.DataFrame()
        if df.empty:
            return df
        if "end_date" in df.columns:
            return df.sort_values("end_date", ascending=False).head(1).copy()
        return df.head(1).copy()


class MarketRegimeDetector:
    """
    机构常用的多指数共振框架（简化版）：
    - 上证指数
    - 沪深300
    - 中证全指
    """

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

        # 多指数投票优先，其次看总分偏向；并采用“偏多优先”避免主线行情被误判过空
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
        """
        蓄势上涨分：越接近放量突破且波动收敛，得分越高
        """
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
        """10日涨幅，过高代表短线拥挤"""
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 12:
            return np.nan
        return float(df["close"].iloc[-1] / df["close"].iloc[-11] - 1)

    def near_high_20d_ratio(self, code: str) -> float:
        """当前价 / 20日最高价，越接近1越容易追高"""
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 25:
            return np.nan
        h20 = float(df["high"].rolling(20).max().iloc[-1])
        c = float(df["close"].iloc[-1])
        if h20 <= 0:
            return np.nan
        return c / h20

    def macd_hist_slope(self, code: str) -> float:
        """
        MACD柱体斜率（近5日）：负值代表红柱收敛/动能衰减
        """
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
        """
        统计最近 lookback_days 内 MACD 柱体连续放大天数（按柱体数值上升计）。
        """
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
        """
        OBV近20日斜率：价格横盘时若OBV抬升，常见主力吸筹迹象
        """
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
        """
        量能收缩分(0~100): 越像"缩量盘整 → 即将启动"得分越高。

        - vol_5_20 = 5日均量 / 20日均量, 理想区间 [0.7, 1.0]
          * <0.7  : 过度缩量(可能换手枯竭, 启动质量打折)
          * [0.7,1.0]: 缩量盘整(正分)
          * (1.0,1.4]: 量能开始抬头(过渡, 中性偏正)
          * >1.4   : 已放量(可能已启动或追高), 降分
        - 在 [0.7, 1.0] 给满分,边缘衰减; 用三角形函数刻画"区间内最优"。
        """
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
        # 1.0 < ratio < 1.6
        return float(np.clip((1.6 - ratio) / (1.6 - 1.0) * 100.0, 0.0, 100.0))

    def amplitude_contraction_pct(self, code: str, window: int = 60) -> float:
        """
        振幅收敛分位(0~1): 近5日平均振幅 (high-low)/close 在过去 window 日的分位。
        分位越低代表"近期振幅收敛"越显著, 是盘整后期的典型形态。
        返回值越小越好(更收敛); 调用方需做反向打分。
        """
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
        """
        均线粘合度分(0~100): 看 MA5/MA10/MA20 三线最大相对距离, 越粘合得分越高。
        粘合后即将发散是"盘整结束"的常见形态。
        """
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
        # spread 0% -> 100, 3% 以上 -> 0, 线性衰减
        score = float(np.clip((0.03 - spread) / 0.03 * 100.0, 0.0, 100.0))
        return score

    def box_position_60d(self, code: str) -> float:
        """
        箱体位置：当前收盘在近60日（不含今日）高低区间的位置，越低越接近箱体底部。
        突破时值会大于1。
        """
        df = self.stock_dfs.get(code, pd.DataFrame())
        if len(df) < 65:
            return np.nan
        # 使用 shift(1) 剔除今日数据，取过去 60 日的历史极值
        h = float(df["high"].shift(1).rolling(60).max().iloc[-1])
        l = float(df["low"].shift(1).rolling(60).min().iloc[-1])
        c = float(df["close"].iloc[-1])
        if pd.isna(h) or pd.isna(l) or h <= l:
            return np.nan
        return (c - l) / (h - l)

    def williams_r(self, code: str, period: int = 14) -> float:
        """
        Williams %R 指标（0~100，越小越强）。
        """
        df = self.stock_dfs.get(code, pd.DataFrame())
        n = int(period)
        if n < 2 or len(df) < n + 2:
            return np.nan
        hh = float(df["high"].tail(n).max())
        ll = float(df["low"].tail(n).min())
        c = float(df["close"].iloc[-1])
        if hh <= ll:
            return np.nan
        # 传统W&R为[-100,0]，这里转为[0,100]便于直观比较
        return float((hh - c) / (hh - ll) * 100)

    def williams_r_down_days(self, code: str, period: int = 14, lookback_days: int = 3) -> int:
        """
        统计近lookback_days内，W&R日序列连续走低(数值变小)的天数。
        """
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
        """
        W&R 趋势分（0~100）：
        - 快线位置不过热（越低越好）
        - 快线相对慢线走强
        - 近N天快线持续走低（数值变小）
        """
        wr_fast = self.williams_r(code, fast_period)
        wr_slow = self.williams_r(code, slow_period)
        down_days = self.williams_r_down_days(code, period=fast_period, lookback_days=lookback_days)

        if pd.isna(wr_fast):
            return np.nan

        # 位置分：wr_fast越低越强，0分界点100，80分界点约20
        level_score = np.clip((100 - wr_fast) / 80 * 100, 0, 100)

        # 快慢线相对强度：fast < slow 代表短周期更强
        rel_score = 50.0
        if pd.notna(wr_slow):
            diff = wr_slow - wr_fast
            rel_score = np.clip(50 + diff * 1.5, 0, 100)

        # 连续走低分：down_days越多越好
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
        """
        金叉相关特征:
        - pre_cross: MA5仍在MA10下方，但差距持续收敛
        - just_cross: 最近1天刚发生MA5上穿MA10
        """
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
            gap_near_min = bool(
                pd.notna(gap_min_recent)
                and gap_min_recent > 0
                and now_gap <= gap_min_recent * float(near_min_ratio)
            )
        else:
            gap_near_min = False
        shrink_ratio = np.nan
        if prev_gap > 0:
            shrink_ratio = now_gap / prev_gap
        is_pre = bool(
            now_gap > 0
            and pd.notna(shrink_ratio)
            and shrink_ratio < float(is_pre_cross_shrink_ratio_max)
        )
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
            out = pd.Series(50.0, index=s.index)
            return out
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
        # 短期过热约束(行业层面估值已在 hybrid 模式下用 5y 分位重排,此处不重复)。
        # 把 runup_penalty 权重从 0.05 提到 0.15, 拉开"刚启动" vs "已涨多天"的分差。
        if not self.meta_df.empty:
            merged = out.merge(self.meta_df[["ts_code", "industry"]], on="ts_code", how="left")
            merged = merged.merge(
                pd.DataFrame(
                    {
                        "ts_code": stock_codes,
                        "runup_10d": [self.calc.runup_10d(c) for c in stock_codes],
                    }
                ),
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


# ── 技术面结构加减分（均线×布林 + MACD，A/B 池共用、矩阵分池）────────
_SCORE_TECH_MAX = 25.0
_SCORE_THEORETICAL_MAX = 80.0


def _bb_vol_state(bb_ratio: float) -> str:
    if pd.isna(bb_ratio):
        return "中性"
    if bb_ratio > 1.05:
        return "发散"
    if bb_ratio < 0.88:
        return "收敛"
    return "中性"


def _macd_hist_dispersion_ratio(close: pd.Series) -> float:
    """MACD 柱体发散度：近5日 |hist| 均值 / 近20日，>1 发散，<1 收敛。"""
    c = pd.to_numeric(close, errors="coerce").dropna()
    if len(c) < 35:
        return np.nan
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()
    h5 = hist.tail(5).abs().mean()
    h20 = hist.tail(20).abs().mean()
    if not pd.notna(h20) or h20 <= 1e-12:
        return 1.0
    return float(h5 / h20)


def _ma_cross_state(close: pd.Series) -> tuple[str, bool]:
    """返回 (金叉|死叉|粘合, 是否近3日内发生交叉)。"""
    c = pd.to_numeric(close, errors="coerce").dropna()
    if len(c) < 15:
        return "粘合", False
    ma5 = c.rolling(5).mean()
    ma10 = c.rolling(10).mean()
    m5, m10 = float(ma5.iloc[-1]), float(ma10.iloc[-1])
    if not np.isfinite(m5) or not np.isfinite(m10) or m10 <= 0:
        return "粘合", False
    gap_pct = abs(m5 - m10) / m10
    if gap_pct < 0.004:
        return "粘合", False
    tag = "金叉" if m5 >= m10 else "死叉"
    recent_flip = False
    for i in range(-3, 0):
        if pd.notna(ma5.iloc[i - 1]) and pd.notna(ma10.iloc[i - 1]):
            was_above = ma5.iloc[i - 1] >= ma10.iloc[i - 1]
            now_above = ma5.iloc[i] >= ma10.iloc[i]
            if was_above != now_above:
                recent_flip = True
                break
    return tag, recent_flip


def _wr_rebound_base_score(wr14: float, signal_day: str) -> float:
    """A 池基础分：WR 下穿后的反弹质量（0~20）。"""
    if not signal_day or not pd.notna(wr14):
        return 0.0
    if 30.0 <= wr14 <= 70.0:
        return 20.0
    if 20.0 <= wr14 < 30.0 or 70.0 < wr14 <= 85.0:
        return 12.0
    return 6.0


def _tech_structure_adjust(
    ma_tag: str,
    bb_state: str,
    macd_ratio: float,
    pos_20d: float,
    ret_20d: float,
    pool: str = "B",
) -> tuple[float, str]:
    """
    均线×布林 矩阵 + MACD 连续项。A 池偏「超卖蓄势」，B 池偏「成长趋势」。
    """
    adj = 0.0
    combo = f"{ma_tag}+{bb_state}"
    is_a = pool == "A"

    if is_a:
        if ma_tag == "死叉":
            if bb_state == "收敛":
                adj += 14.0
            elif bb_state == "发散":
                adj -= 10.0
            else:
                adj -= 5.0
        elif ma_tag == "金叉":
            if bb_state == "收敛":
                adj += 8.0
            elif bb_state == "发散":
                adj += 1.0
                if pd.notna(pos_20d) and pos_20d > 0.88:
                    adj -= min(12.0, (pos_20d - 0.88) / 0.12 * 12.0)
        else:
            adj += 4.0
        if pd.notna(macd_ratio):
            if macd_ratio > 1.0:
                adj -= min(8.0, (macd_ratio - 1.0) * 12.0)
            else:
                adj += min(10.0, (1.0 - macd_ratio) * 14.0)
    else:
        if ma_tag == "死叉":
            if bb_state == "发散":
                adj -= 16.0
            elif bb_state == "收敛":
                adj += 10.0
            else:
                adj -= 8.0
        elif ma_tag == "金叉":
            if bb_state == "收敛":
                adj += 12.0
            elif bb_state == "发散":
                adj += 4.0
                if pd.notna(pos_20d) and pos_20d > 0.85:
                    adj -= min(14.0, (pos_20d - 0.85) / 0.15 * 14.0)
                if pd.notna(ret_20d) and ret_20d > 25.0:
                    adj -= min(8.0, (ret_20d - 25.0) / 25.0 * 8.0)
            else:
                adj += 5.0
        else:
            if bb_state == "收敛":
                adj += 4.0
            elif bb_state == "发散":
                adj -= 3.0
        if pd.notna(macd_ratio):
            if macd_ratio > 1.0:
                adj -= min(12.0, (macd_ratio - 1.0) * 18.0)
            else:
                adj += min(8.0, (1.0 - macd_ratio) * 12.0)

    adj = float(np.clip(adj, -_SCORE_TECH_MAX, _SCORE_TECH_MAX))
    return adj, combo


def _pool_kind_from_row(pool_type: str) -> str:
    return "A" if "超卖" in str(pool_type or "") else "B"


def run_daily_selection(token: str, top_n: int = 8) -> pd.DataFrame:
    # ─────────────────────────────────────────────────────────────
    # 双池并行：版本A — 超卖反弹 + 版本B — 科技成长
    # ─────────────────────────────────────────────────────────────
    today = datetime.now().date()
    raw_end_date = today.strftime("%Y-%m-%d")
    fetcher = DataFetcher(token=token)
    end_date = fetcher.get_latest_trade_date(raw_end_date)

    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    end_ymd = end_dt.strftime("%Y%m%d")
    start_260 = (end_dt.date() - timedelta(days=420)).strftime("%Y-%m-%d")
    start_5y = (end_dt - timedelta(days=365 * 5 + 20)).strftime("%Y-%m-%d")

    basic = fetcher.get_stock_basic_mainboard()
    universe = basic[["ts_code", "name", "industry"]].drop_duplicates(subset=["ts_code"]).copy()
    codes_all = universe["ts_code"].astype(str).tolist()
    print(f"选股日期: {end_date} | 全市场候选(剔ST/北交等): {len(codes_all)}")

    # ── 公共数据：日线基础 + 同花顺概念映射 ──
    daily_basic_today = fetcher.get_daily_basic(codes_all, end_date)
    daily_basic_today["pe_ttm"] = pd.to_numeric(daily_basic_today.get("pe_ttm", np.nan), errors="coerce")
    daily_basic_today["total_mv"] = pd.to_numeric(daily_basic_today.get("total_mv", np.nan), errors="coerce")

    ths_map: dict[str, str] = {}
    try:
        idx = fetcher.pro.ths_index()
        if idx is not None and not idx.empty and "ts_code" in idx.columns and "name" in idx.columns:
            idx = idx.copy()
            idx["ts_code"] = idx["ts_code"].astype(str)
            idx["name"] = idx["name"].astype(str)
            idx_pick = idx.head(200)
            pairs: list[tuple[str, str]] = []
            for _, r in idx_pick.iterrows():
                c = str(r["ts_code"])
                nm = str(r["name"])
                try:
                    m = fetcher.pro.ths_member(ts_code=c)
                except Exception:
                    continue
                if m is None or m.empty or "con_code" not in m.columns:
                    continue
                for code in m["con_code"].astype(str).tolist():
                    pairs.append((code, nm))
            if pairs:
                inv = pd.DataFrame(pairs, columns=["ts_code", "concept1"])
                inv = inv.sort_values(["ts_code", "concept1"]).drop_duplicates(subset=["ts_code"], keep="first")
                ths_map = dict(zip(inv["ts_code"], inv["concept1"]))
    except Exception:
        ths_map = {}

    def _apply_ths(df: pd.DataFrame) -> pd.DataFrame:
        df["ths_concept1"] = df["ts_code"].map(lambda x: ths_map.get(str(x), ""))
        df["concept"] = df["ths_concept1"].fillna("")
        return df

    # ── 布林带宽度计算（复用）──
    def _bb_width_metrics(code: str) -> tuple[float, float]:
        df = fetcher.get_stock_daily(code, start_260, end_date)
        if df is None or df.empty or "close" not in df.columns:
            return (np.nan, np.nan)
        c = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(c) < 260:
            return (np.nan, np.nan)
        ma20 = c.rolling(20).mean()
        std20 = c.rolling(20).std()
        upper = ma20 + 2 * std20
        lower = ma20 - 2 * std20
        width = (upper - lower) / ma20.replace(0, np.nan)
        width = width.dropna()
        if len(width) < 250:
            return (np.nan, np.nan)
        cur = float(width.iloc[-1])
        mean250 = float(width.tail(250).mean())
        return (cur, mean250)

    def _bb_filter(df_in: pd.DataFrame, quantile: float = 0.30) -> pd.DataFrame:
        codes = df_in["ts_code"].astype(str).tolist()
        bb_cur = []
        bb_mean = []
        for c in codes:
            cur, m250 = _bb_width_metrics(c)
            bb_cur.append(cur)
            bb_mean.append(m250)
        df_in["bb_width_cur"] = bb_cur
        df_in["bb_width_mean250"] = bb_mean
        df_in["bb_width_ratio"] = df_in["bb_width_cur"] / df_in["bb_width_mean250"].replace(0, np.nan)
        th = float(pd.to_numeric(df_in["bb_width_ratio"], errors="coerce").quantile(quantile))
        if not np.isfinite(th):
            th = 0.9
        return df_in[df_in["bb_width_ratio"].notna() & (df_in["bb_width_ratio"] <= th)].copy()

    def _turnover_60d_avg(code: str) -> float:
        hist = fetcher.pro.daily_basic(
            ts_code=code,
            start_date=fetcher._to_ts_date((end_dt.date() - timedelta(days=120)).strftime("%Y-%m-%d")),
            end_date=fetcher._to_ts_date(end_date),
            fields="trade_date,ts_code,turnover_rate",
        )
        if hist is None or hist.empty or "turnover_rate" not in hist.columns:
            return np.nan
        x = pd.to_numeric(hist["turnover_rate"], errors="coerce").dropna().tail(60)
        if len(x) < 30:
            return np.nan
        return float(x.mean())

    def _calc_turnover(df_in: pd.DataFrame) -> pd.DataFrame:
        codes = df_in["ts_code"].astype(str).tolist()
        df_in["turnover_60d_avg"] = [ _turnover_60d_avg(c) for c in codes ]
        return df_in

    _FINA_FIELDS = "roe,roe_dt,or_yoy,end_date"
    _fina_cache: dict[str, dict[str, float]] = {}  # code -> {roe: float, rev_yoy: float}

    def _prefetch_fina(codes: list[str]) -> None:
        """批量预取财务指标，限流 0.35s/次，结果写入 _fina_cache。"""
        import time as _time
        for i, code in enumerate(codes):
            if code in _fina_cache:
                continue
            try:
                fi = fetcher.get_fina_indicator_latest(code, fields=_FINA_FIELDS)
            except Exception:
                fi = pd.DataFrame()
            row: dict[str, float] = {}
            if fi is not None and not fi.empty:
                for col, out_key in [
                    ("roe_dt", "roe"), ("roe", "roe"),
                    ("or_yoy", "rev_yoy"),
                ]:
                    if col in fi.columns:
                        v = pd.to_numeric(fi[col].iloc[0], errors="coerce")
                        if pd.notna(v) and out_key not in row:
                            row[out_key] = float(v)
            _fina_cache[code] = row
            # 限流避免打满 Tushare 频率上限
            if (i + 1) % 10 == 0:
                _time.sleep(0.5)
            else:
                _time.sleep(0.35)
        print(f"[fina] 预取完成：{len(codes)} 支，命中 {sum(1 for v in _fina_cache.values() if v)} 支有数据")

    def _roe_latest(code: str) -> float:
        if code in _fina_cache:
            return _fina_cache[code].get("roe", np.nan)
        fi = fetcher.get_fina_indicator_latest(code)
        if fi is None or fi.empty:
                return np.nan
        for col in ("roe_dt", "roe"):
            if col in fi.columns:
                v = pd.to_numeric(fi[col].iloc[0], errors="coerce")
                if pd.notna(v):
                    return float(v)
        return np.nan

    def _q_rev_yu(code: str) -> float:
        """营收同比增长率（优先单季度，其次全年）"""
        if code in _fina_cache:
            return _fina_cache[code].get("rev_yoy", np.nan)
        fi = fetcher.get_fina_indicator_latest(code)
        if fi is None or fi.empty:
            return np.nan
        for col in ("or_yoy", "q_gr_yoy"):
            if col in fi.columns:
                v = pd.to_numeric(fi[col].iloc[0], errors="coerce")
                if pd.notna(v):
                    return float(v)
        return np.nan

    def _score_pool(df_in: pd.DataFrame) -> pd.DataFrame:
        """
        统一打分（A/B 均为基础策略的一部分）：
        - A 池：WR 质量(20) + 布林(25) + ROE(10) + 技术结构/MACD(±25)
        - B 池：布林(40) + ROE(15) + 技术结构/MACD(±25)
        """
        base_list: list[float] = []
        tech_adj_list: list[float] = []
        ma_tags: list[str] = []
        bb_states: list[str] = []
        macd_ratios: list[float] = []
        tech_combos: list[str] = []

        hist_start = (end_dt.date() - timedelta(days=120)).strftime("%Y-%m-%d")
        for _, row in df_in.iterrows():
            code = str(row["ts_code"])
            pool = _pool_kind_from_row(str(row.get("pool_type", "")))
            bb_ratio = float(row["bb_width_ratio"]) if pd.notna(row.get("bb_width_ratio")) else np.nan
            bb_st = _bb_vol_state(bb_ratio)
            roe_v = float(pd.to_numeric(row.get("roe"), errors="coerce")) if pd.notna(row.get("roe")) else 0.0
            bw_row = bb_ratio if pd.notna(bb_ratio) else 1.0

            if pool == "A":
                wr_v = pd.to_numeric(row.get("wr14_now"), errors="coerce")
                sig = str(row.get("signal_day") or "")
                base = _wr_rebound_base_score(
                    float(wr_v) if pd.notna(wr_v) else np.nan, sig
                )
                base += (1.0 - min(bw_row, 1.0)) * 25.0
                base += min(roe_v / 20.0, 1.0) * 10.0
            else:
                base = (1.0 - min(bw_row, 1.0)) * 40.0
                base += min(roe_v / 20.0, 1.0) * 15.0

            df_d = fetcher.get_stock_daily(code, hist_start, end_date)
            if df_d is None or df_d.empty or "close" not in df_d.columns:
                ma_tag, macd_r, pos20, ret20 = "粘合", np.nan, np.nan, np.nan
            else:
                df_d = df_d.sort_index()
                close = pd.to_numeric(df_d["close"], errors="coerce")
                ma_tag, _ = _ma_cross_state(close)
                macd_r = _macd_hist_dispersion_ratio(close)
                if len(close) >= 21:
                    h20 = float(pd.to_numeric(df_d["high"], errors="coerce").tail(20).max())
                    pos20 = float(close.iloc[-1] / h20) if h20 > 0 else np.nan
                    ret20 = float(close.iloc[-1] / close.iloc[-21] - 1) * 100
                else:
                    pos20, ret20 = np.nan, np.nan

            adj, combo = _tech_structure_adjust(ma_tag, bb_st, macd_r, pos20, ret20, pool=pool)
            base_list.append(float(base))
            tech_adj_list.append(adj)
            ma_tags.append(ma_tag)
            bb_states.append(bb_st)
            macd_ratios.append(macd_r if pd.notna(macd_r) else np.nan)
            tech_combos.append(combo)

        total = (
            pd.Series(base_list, index=df_in.index)
            + pd.Series(tech_adj_list, index=df_in.index)
        ).clip(0, _SCORE_THEORETICAL_MAX)
        df_in["score_base"] = pd.Series(base_list, index=df_in.index).round(4)
        df_in["tech_adj"] = pd.Series(tech_adj_list, index=df_in.index).round(4)
        df_in["ma_cross"] = ma_tags
        df_in["bb_state"] = bb_states
        df_in["macd_disp_ratio"] = macd_ratios
        df_in["tech_combo"] = tech_combos
        df_in["score_raw"] = total.round(4)
        df_in["score_norm"] = (total / _SCORE_THEORETICAL_MAX * 100.0).clip(0, 100).round(1)
        return df_in

    # ─────────────────────────────────────────────
    # 版本A：超卖反弹（原4规则—已放宽阈值）
    # ─────────────────────────────────────────────
    print("\n===== 版本A：超卖反弹 =====")
    pe_ok = daily_basic_today[
        daily_basic_today["pe_ttm"].notna()
        & (daily_basic_today["pe_ttm"] >= 0)
        & (daily_basic_today["pe_ttm"] <= 80)
    ][["ts_code", "pe_ttm"]].copy()
    a1 = universe.merge(pe_ok, on="ts_code", how="inner")
    print(f"A-1 PE 0-80：{len(a1)}")
    if a1.empty:
        pool_a = pd.DataFrame()
    else:
        a1 = _calc_turnover(a1)
        a2 = a1[a1["turnover_60d_avg"].notna() & (a1["turnover_60d_avg"] >= 0.8) & (a1["turnover_60d_avg"] <= 5.0)].copy()
        print(f"A-2 换手 0.8%-5%：{len(a2)}")
        if a2.empty:
            pool_a = pd.DataFrame()
        else:
            a3 = _bb_filter(a2, quantile=0.30)
            print(f"A-3 布林带收敛(30%)：{len(a3)}")
            if a3.empty:
                pool_a = pd.DataFrame()
            else:
                # WR14信号
                def _wr14_series(df, period=14):
                    h = pd.to_numeric(df["high"], errors="coerce")
                    l = pd.to_numeric(df["low"], errors="coerce")
                    c = pd.to_numeric(df["close"], errors="coerce")
                    hh = h.rolling(period).max()
                    ll = l.rolling(period).min()
                    wr = (hh - c) / (hh - ll).replace(0, np.nan) * 100
                    return wr
                sig_ok = []
                sig_day = []
                wr_now = []
                for c in a3["ts_code"].astype(str).tolist():
                    df = fetcher.get_stock_daily(c, (end_dt.date() - timedelta(days=80)).strftime("%Y-%m-%d"), end_date)
                    ok, day, wrv = False, "", np.nan
                    if df is not None and not df.empty and len(df) >= 25:
                        df = df.sort_index()
                        wr14 = _wr14_series(df, 14)
                        ma5 = pd.to_numeric(df["close"], errors="coerce").rolling(5).mean()
                        close = pd.to_numeric(df["close"], errors="coerce")
                        if len(wr14.dropna()) >= 3:
                            t, y, dby = -1, -2, -3
                            def _ok(i_from, i_to):
                                return (pd.notna(wr14.iloc[i_from]) and pd.notna(wr14.iloc[i_to])
                                        and pd.notna(close.iloc[i_to]) and pd.notna(ma5.iloc[i_to])
                                        and (wr14.iloc[i_from] > 80) and (wr14.iloc[i_to] < 80)
                                        and (close.iloc[i_to] >= ma5.iloc[i_to]))
                            if _ok(y, t):
                                ok, day, wrv = True, "today", float(wr14.iloc[t])
                            elif _ok(dby, y):
                                ok, day, wrv = True, "yesterday", float(wr14.iloc[y])
                    sig_ok.append(ok)
                    sig_day.append(day)
                    wr_now.append(wrv)
                a3["wr14_now"] = wr_now
                a3["signal_day"] = sig_day
                a4 = a3[pd.Series(sig_ok, index=a3.index)].copy()
                print(f"A-4 WR14信号：{len(a4)}")
                if a4.empty:
                    pool_a = pd.DataFrame()
                else:
                    a4["roe"] = [ _roe_latest(c) for c in a4["ts_code"].astype(str).tolist() ]
                    a4["roe_pass"] = a4["roe"].notna() & (a4["roe"] > 5.0)
                    a4["pool_type"] = "A-超卖反弹"
                    pool_a = _apply_ths(a4)

    # ─────────────────────────────────────────────
    # 版本B：科技成长
    # ─────────────────────────────────────────────
    print("\n===== 版本B：科技成长 =====")
    # B-1：换手 2%-6%（更活跃）
    b1 = _calc_turnover(universe)
    b1 = b1[b1["turnover_60d_avg"].notna() & (b1["turnover_60d_avg"] >= 2.0) & (b1["turnover_60d_avg"] <= 6.0)].copy()
    print(f"B-1 换手 2%-6%：{len(b1)}")
    if b1.empty:
        pool_b = pd.DataFrame()
    else:
        # B-2：营收增速 > 20%（先批量预取, 限流避免打满频率上限）
        rev_codes = b1["ts_code"].astype(str).tolist()
        _prefetch_fina(rev_codes)
        b1["q_rev_yu"] = [ _q_rev_yu(c) for c in rev_codes ]
        b2 = b1[b1["q_rev_yu"].notna() & (b1["q_rev_yu"] > 20.0)].copy()
        print(f"B-2 营收增速>20%：{len(b2)}")
        if b2.empty:
            pool_b = pd.DataFrame()
        else:
            # B-3：布林带收敛（30%分位，同版本A口径）
            b3 = _bb_filter(b2, quantile=0.30)
            print(f"B-3 布林带收敛(30%)：{len(b3)}")
            if b3.empty:
                pool_b = pd.DataFrame()
            else:
                # B-4：量比 > 1.0（近3日相对活跃，收敛期放宽阈值）
                codes_b4 = b3["ts_code"].astype(str).tolist()
                vol_ratio = []
                for c in codes_b4:
                    df = fetcher.get_stock_daily(c, (end_dt.date() - timedelta(days=45)).strftime("%Y-%m-%d"), end_date)
                    if df is None or df.empty or "volume" not in df.columns or len(df) < 4:
                        vol_ratio.append(np.nan)
                    else:
                        v = pd.to_numeric(df["volume"], errors="coerce").dropna()
                        if len(v) < 4:
                            vol_ratio.append(np.nan)
                        else:
                            tail_v = v.tail(20)
                            denom = tail_v.mean()
                            if pd.isna(denom) or denom <= 1e-9:
                                vol_ratio.append(np.nan)
                            else:
                                vol_ratio.append(float(v.tail(3).mean() / denom))
                b3["vol_ratio_3d"] = vol_ratio
                b4 = b3[b3["vol_ratio_3d"].notna() & (b3["vol_ratio_3d"] >= 1.0)].copy()
                print(f"B-4 量比>1.0：{len(b4)}")
                if b4.empty:
                    pool_b = pd.DataFrame()
                else:
                    b4["roe"] = [ _roe_latest(c) for c in b4["ts_code"].astype(str).tolist() ]
                    b4["roe_pass"] = b4["roe"].notna() & (b4["roe"] > 5.0)
                    b4["pool_type"] = "B-科技成长"
                    b4["wr14_now"] = np.nan
                    b4["signal_day"] = ""
                    pool_b = _apply_ths(b4)

    # ─────────────────────────────────────────────
    # 合并双池 + 打分 + 导出
    # ─────────────────────────────────────────────
    parts = []
    if not pool_a.empty:
        parts.append(pool_a)
    if not pool_b.empty:
        parts.append(pool_b)
    if not parts:
        print("\n⚠ 双池均为空，输出空结果。")
        return pd.DataFrame()

    final_df = pd.concat(parts, ignore_index=True)
    for c in ["bb_width_cur", "bb_width_mean250", "bb_width_ratio"]:
        if c not in final_df.columns:
            final_df[c] = np.nan

    final_df = _score_pool(final_df)

    # 输出字段
    final_df["strategy"] = "量化蓄势突破"
    final_df["strategy_short"] = "蓄势"
    final_df["latest_trade_date"] = end_ymd
    final_df["pe_ttm"] = final_df["ts_code"].map(
        lambda x: float(daily_basic_today[daily_basic_today["ts_code"] == x]["pe_ttm"].iloc[0])
        if x in daily_basic_today["ts_code"].values else np.nan
    )
    final_df["turnover_rate"] = final_df["ts_code"].map(
        lambda x: float(daily_basic_today[daily_basic_today["ts_code"] == x]["turnover_rate"].iloc[0])
        if x in daily_basic_today["ts_code"].values else np.nan
    )

    final_df = final_df.sort_values(["pool_type", "score_norm"], ascending=[True, False]).reset_index(drop=True)
    # 双池均衡：各取 top_n//2，确保 B 池不被 A 池挤出
    half = max(int(top_n) // 2, 2)
    parts_ab = []
    for pt in ["A-超卖反弹", "B-科技成长"]:
        sub = final_df[final_df["pool_type"] == pt].head(half)
        if not sub.empty:
            parts_ab.append(sub)
    out = pd.concat(parts_ab, ignore_index=True) if parts_ab else final_df.head(max(int(top_n), 1))

    out_dir = Path("output/daily")
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = str(end_date).replace("-", "")
    export_suffix = os.getenv("QUANT_EXPORT_SUFFIX", "").strip()
    file_mid = f"daily_selection_{tag}{export_suffix}"
    csv_en = out_dir / f"{file_mid}.csv"
    csv_cn = out_dir / f"{file_mid}_cn.csv"
    out.to_csv(csv_en, index=False, encoding="utf-8-sig")
    to_chinese_columns(out.copy()).to_csv(csv_cn, index=False, encoding="utf-8-sig")
    print(f"\n已导出: {csv_en.resolve()}")
    print(f"        {csv_cn.resolve()}")

    return out


if __name__ == "__main__":
    # Token 使用环境变量（不写进仓库）；本机已在 ~/.zshrc 或 IDE 里 export 则无需每次设置。
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        raise ValueError("请先设置环境变量 TUSHARE_TOKEN。")

    run_daily_selection(token=token, top_n=8)

    root = Path(__file__).resolve().parent
    dash_script = root / "build_dashboard.py"
    if dash_script.exists():
        import subprocess

        try:
            proc = subprocess.run(
                [sys.executable, str(dash_script)],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=180,
            )
            if proc.returncode == 0 and proc.stdout:
                last = proc.stdout.strip().split("\n")[-1]
                print(last)
            elif proc.returncode != 0:
                print(
                    "提示: 看板未自动生成，请手动执行: python build_dashboard.py",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"提示: 看板生成跳过 ({exc})，可手动运行 python build_dashboard.py", file=sys.stderr)