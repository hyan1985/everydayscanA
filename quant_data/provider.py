"""DataProvider — Parquet 优先透明代理。

  接口与 tushare.pro_api() 返回的 ProAPI 对象完全兼容。
  未命中本地 Parquet 时自动回退 Tushare API 并缓存到 Parquet。
"""

from __future__ import annotations

import os
import time
import types
from typing import Any, Optional

import pandas as pd
import tushare as ts

from quant_data import storage as s


# 按 API 名 / 参数模式配置 TTL（秒）
_STATIC_TTL = 86400  # 24h
_MEMBER_TTL = 86400
_DATE_TTL = 21600  # 6h
_PER_STOCK_TTL = 43200  # 12h


# 哪些接口按 stock_basic 级别静态缓存
_STATIC_APIS = frozenset({
    "stock_basic",
    "trade_cal",
    "ths_index",
    "ths_hot",
})

# 哪些是 ths_member 级别
_MEMBER_APIS = frozenset({
    "ths_member",
})

# 哪些是按日期分区的批量数据
_DATE_APIS = frozenset({
    "daily",
    "daily_basic",
    "moneyflow",
    "limit_list_d",
    "index_daily",
    "hm_detail",
    "top_inst",
    "ths_daily",
})

# 哪些是按股票 + 日期的明细数据
_PER_STOCK_APIS = frozenset({
    "stk_factor_pro",
    "fina_indicator",
    "cyq_perf",
    "moneyflow_dc",
    "share_float",
    "stk_holdertrade",
})


class DataProvider:
    """Parquet 优先的 tushare ProAPI 透明代理。"""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        token: Optional[str] = None,
        concepts_path: Optional[str] = None,
    ):
        self._data_dir = data_dir
        self._concepts_path = concepts_path
        self._token = token or os.environ.get("TUSHARE_TOKEN", "")
        self._pro: Optional[Any] = None
        self._hit = 0
        self._miss = 0
        self._from_cache = 0

    # ── lazy tushare pro ──

    @property
    def pro(self):
        if self._pro is None:
            if not self._token:
                from tushare_cache import _load_token
                self._token = os.environ.get("TUSHARE_TOKEN", "")
            self._pro = ts.pro_api(self._token)
        return self._pro

    # ── 入口 ──

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in _STATIC_APIS:
            return self._make_static_handler(name)
        if name in _MEMBER_APIS:
            return self._make_member_handler(name)
        if name in _DATE_APIS:
            return self._make_date_handler(name)
        if name in _PER_STOCK_APIS:
            return self._make_per_stock_handler(name)
        # 未识别的 API → 直接透传 tushare
        return getattr(self.pro, name)

    # ── handler 工厂 ──

    def _make_static_handler(self, api: str):
        """静态数据，按 name 缓存一次。

        trade_cal 例外：必须覆盖到今天，否则策略会基于过期日历选到昨天的数据。
        每次读取时检测日历是否过期，过期则自动重新拉取全量日历并按调用参数过滤。
        """
        def handler(**kwargs) -> Optional[pd.DataFrame]:
            cached = s.read_static(api)

            if api == "trade_cal":
                from quant_data.fetcher import trade_cal_needs_refresh
                if trade_cal_needs_refresh(cached):
                    import datetime as _dt
                    today = _dt.datetime.now().strftime("%Y%m%d")
                    try:
                        fresh = self.pro.trade_cal(
                            exchange="SSE", start_date="20000101", end_date=today
                        )
                    except Exception:
                        fresh = None
                    if fresh is not None and not fresh.empty:
                        s.write_static(api, fresh)
                        cached = fresh
                        self._miss += 1
                    else:
                        self._hit += 1
                else:
                    self._hit += 1
                return _filter_trade_cal(cached, kwargs)

            if cached is not None:
                self._hit += 1
                return cached

            method = getattr(self.pro, api)
            result = method(**kwargs)
            if result is not None and not result.empty:
                s.write_static(api, result)
                self._from_cache += 1
            self._miss += 1
            return result
        return handler

    def _make_member_handler(self, api: str):
        """指数成分股，按指数代码缓存。"""
        def handler(**kwargs) -> Optional[pd.DataFrame]:
            ts_code = kwargs.get("ts_code")
            if not ts_code:
                method = getattr(self.pro, api)
                return method(**kwargs)

            cached = s.read_ths_member(ts_code)
            if cached is not None:
                self._hit += 1
                return cached

            method = getattr(self.pro, api)
            result = method(**kwargs)
            if result is not None and not result.empty:
                s.write_ths_member(ts_code, result)
                self._from_cache += 1
            self._miss += 1
            return result
        return handler

    def _make_date_handler(self, api: str):
        """按交易日分区的批量数据（也支持个股历史 route）。"""
        def handler(**kwargs) -> Optional[pd.DataFrame]:
            ts_code = kwargs.get("ts_code", "")
            trade_date = kwargs.get("trade_date", "")
            start_date = kwargs.get("start_date", "")
            end_date = kwargs.get("end_date", "")

            # ── 个股历史 route（如 daily(ts_code=xxx, start_date=..., end_date=...)）──
            if ts_code and (start_date or end_date):
                full = s.read_per_stock_all(api, ts_code)
                if full is not None and not full.empty and _cache_covers_end(full, end_date):
                    self._hit += 1
                    full["_tds"] = full["trade_date"].astype(str).str.replace("-", "").str[:8]
                    if start_date:
                        full = full[full["_tds"] >= _norm_ymd(start_date)]
                    if end_date:
                        full = full[full["_tds"] <= _norm_ymd(end_date)]
                    return full.drop(columns=["_tds"], errors="ignore")
                # 缓存缺失或未覆盖 end_date → 回退 API 并增量写入
                method = getattr(self.pro, api)
                api_kwargs = dict(kwargs)
                cached_max = _per_stock_max_trade_date(full)
                req_end = _norm_ymd(end_date)
                if cached_max and req_end and cached_max < req_end:
                    api_kwargs["start_date"] = cached_max
                    api_kwargs["end_date"] = req_end
                result = method(**api_kwargs)
                if result is not None and not result.empty:
                    _write_per_stock_by_date(api, ts_code, result)
                    if full is not None and not full.empty:
                        result = pd.concat([full, result], ignore_index=True)
                        result = result.drop_duplicates(subset=["trade_date"], keep="last")
                self._miss += 1
                if result is None or result.empty:
                    return result
                result = result.copy()
                result["_tds"] = result["trade_date"].astype(str).str.replace("-", "").str[:8]
                if start_date:
                    result = result[result["_tds"] >= _norm_ymd(start_date)]
                if end_date:
                    result = result[result["_tds"] <= _norm_ymd(end_date)]
                return result.drop(columns=["_tds"], errors="ignore")

            # ── 按日期分区 route（如 daily(trade_date=YYYYMMDD)）──
            if not trade_date:
                trade_date = end_date
            if not trade_date:
                method = getattr(self.pro, api)
                return method(**kwargs)

            base_dir = _date_base_dir(api)

            # 单股 + 单日：从全市场分区筛选；单股 API 结果只写 per_stock，不写日期分区
            if ts_code and not start_date and not end_date:
                cached = s.read_date_partition(base_dir, trade_date, ttl_sec=_DATE_TTL)
                if cached is not None and not cached.empty and "ts_code" in cached.columns:
                    self._hit += 1
                    sub = cached[cached["ts_code"].astype(str) == str(ts_code)]
                    if sub.empty:
                        pass  # 日期分区无此股票，回退到 Tushare API
                    out = sub.copy()
                    if api == "daily_basic" and (
                        "circ_mv" not in out.columns or out["circ_mv"].isna().all()
                    ):
                        ps = s.read_per_stock(api, ts_code, trade_date)
                        if ps is not None and not ps.empty and "circ_mv" in ps.columns:
                            cv = ps.iloc[0].get("circ_mv")
                            if pd.notna(cv):
                                out.loc[:, "circ_mv"] = cv
                    return out
                method = getattr(self.pro, api)
                result = method(**kwargs)
                if result is not None and not result.empty:
                    s.write_per_stock(api, ts_code, trade_date, result)
                self._miss += 1
                return result if result is not None else pd.DataFrame()

            cached = s.read_date_partition(base_dir, trade_date, ttl_sec=_DATE_TTL)
            if cached is not None and not cached.empty:
                # daily_basic/moneyflow 日期分区可能因不同策略使用不同 fields 而产生字段缺失缓存
                _use_cached = True
                if api in ("daily_basic", "moneyflow"):
                    req_fields = kwargs.get("fields", "")
                    if not req_fields:
                        # 未指定 fields = 需要全部列，缓存可能被部分字段请求污染
                        _use_cached = False
                    else:
                        req_set = set(f.strip() for f in req_fields.split(",") if f.strip())
                        cached_set = set(cached.columns)
                        missing = req_set - cached_set
                        if missing:
                            _use_cached = False  # 缓存字段不完整，回退到 Tushare API 重新拉取
                if _use_cached:
                    self._hit += 1
                    return cached

            method = getattr(self.pro, api)
            result = method(**kwargs)
            if result is not None and not result.empty:
                if s.should_write_market_date_partition(api, base_dir, trade_date, result):
                    s.write_date_partition(base_dir, trade_date, result)
                    self._from_cache += 1
            self._miss += 1
            return result
        return handler

    def _make_per_stock_handler(self, api: str):
        """按股票 + 日期缓存的明细数据。"""
        def handler(**kwargs) -> Optional[pd.DataFrame]:
            ts_code = kwargs.get("ts_code", "")
            trade_date = kwargs.get("trade_date") or ""
            start_date = kwargs.get("start_date", "")
            end_date = kwargs.get("end_date", "")

            # 单日期查询
            if ts_code and trade_date:
                cached = s.read_per_stock(api, ts_code, trade_date)
                if cached is not None:
                    self._hit += 1
                    return cached
                method = getattr(self.pro, api)
                result = method(**kwargs)
                if result is not None and not result.empty:
                    s.write_per_stock(api, ts_code, trade_date, result)
                    self._from_cache += 1
                self._miss += 1
                return result

            # 整段历史（优先从本地读取全部）
            if ts_code and start_date and end_date:
                full = s.read_per_stock_all(api, ts_code)
                if full is not None and not full.empty and _cache_covers_end(full, end_date):
                    self._hit += 1
                    full["trade_date_str"] = full["trade_date"].astype(str).str.replace("-", "").str[:8]
                    sd, ed = _norm_ymd(start_date), _norm_ymd(end_date)
                    mask = (full["trade_date_str"] >= sd) & (full["trade_date_str"] <= ed)
                    return full[mask].drop(columns=["trade_date_str"], errors="ignore")
                method = getattr(self.pro, api)
                api_kwargs = dict(kwargs)
                cached_max = _per_stock_max_trade_date(full)
                req_end = _norm_ymd(end_date)
                if cached_max and req_end and cached_max < req_end:
                    api_kwargs["start_date"] = cached_max
                    api_kwargs["end_date"] = req_end
                result = method(**api_kwargs)
                if result is not None and not result.empty:
                    _write_per_stock_by_date(api, ts_code, result)
                    if full is not None and not full.empty:
                        result = pd.concat([full, result], ignore_index=True)
                        result = result.drop_duplicates(subset=["trade_date"], keep="last")
                self._miss += 1
                if result is None or result.empty:
                    return result
                result = result.copy()
                result["trade_date_str"] = result["trade_date"].astype(str).str.replace("-", "").str[:8]
                sd, ed = _norm_ymd(start_date), _norm_ymd(end_date)
                mask = (result["trade_date_str"] >= sd) & (result["trade_date_str"] <= ed)
                return result[mask].drop(columns=["trade_date_str"], errors="ignore")

            # 既无 ts_code 也无 trade_date → 直接透传
            method = getattr(self.pro, api)
            return method(**kwargs)
        return handler

    # ── 统计 ──

    def stats(self) -> dict:
        root = s.data_root()
        parquet_count = len(list(root.rglob("*.parquet")))
        try:
            size_mb = sum(f.stat().st_size for f in root.rglob("*.parquet")) / 1048576
        except OSError:
            size_mb = 0
        return {
            "parquet_files": parquet_count,
            "size_mb": round(size_mb, 1),
            "session_hit": self._hit,
            "session_miss": self._miss,
            "from_cache": self._from_cache,
        }

    def print_stats(self):
        st = self.stats()
        print(
            f"[quant_data] 本地读取 {st['session_hit']} / 回退API {st['session_miss']} "
            f"| 本地文件 {st['parquet_files']} 个 / {st['size_mb']}MB"
        )


def _norm_ymd(value: str) -> str:
    return str(value or "").replace("-", "").strip()[:8]


def _per_stock_max_trade_date(full: Optional[pd.DataFrame]) -> str:
    if full is None or full.empty or "trade_date" not in full.columns:
        return ""
    return str(full["trade_date"].astype(str).str.replace("-", "").str[:8].max())


def _cache_covers_end(full: Optional[pd.DataFrame], end_date: str) -> bool:
    """个股历史缓存是否覆盖请求的 end_date（未指定 end_date 则视为已覆盖）。"""
    req_end = _norm_ymd(end_date)
    if not req_end:
        return True
    cached_max = _per_stock_max_trade_date(full)
    return bool(cached_max) and cached_max >= req_end


def _write_per_stock_by_date(api: str, ts_code: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    tmp = df.copy()
    tmp["_td"] = tmp["trade_date"].astype(str).str.replace("-", "").str[:8]
    for td, chunk in tmp.groupby("_td"):
        if len(td) == 8 and td.isdigit():
            s.write_per_stock(api, ts_code, td, chunk.drop(columns=["_td"]))


def _filter_trade_cal(df: Optional[pd.DataFrame], kwargs: dict) -> Optional[pd.DataFrame]:
    """按调用参数过滤交易日历（缓存存全量，读取时按 exchange/is_open/日期区间过滤）。"""
    if df is None or df.empty:
        return df
    out = df
    ex = kwargs.get("exchange")
    if ex and "exchange" in out.columns:
        out = out[out["exchange"].astype(str) == str(ex)]
    sd = kwargs.get("start_date")
    if sd and "cal_date" in out.columns:
        out = out[out["cal_date"].astype(str) >= str(sd)]
    ed = kwargs.get("end_date")
    if ed and "cal_date" in out.columns:
        out = out[out["cal_date"].astype(str) <= str(ed)]
    io = kwargs.get("is_open")
    if io is not None and io != "" and "is_open" in out.columns:
        try:
            out = out[out["is_open"].astype(int) == int(io)]
        except (ValueError, TypeError):
            pass
    return out.copy()


def _date_base_dir(api: str) -> Path:
    mapping = {
        "daily": s.daily_dir(),
        "daily_basic": s.daily_basic_dir(),
        "moneyflow": s.moneyflow_dir(),
        "limit_list_d": s.data_root() / "limit_list_d",
        "index_daily": s.data_root() / "index_daily",
        "hm_detail": s.data_root() / "hm_detail",
        "top_inst": s.data_root() / "top_inst",
        "ths_daily": s.data_root() / "ths_daily",
    }
    return mapping.get(api, s.data_root() / api)
