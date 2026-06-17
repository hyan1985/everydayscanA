"""Parquet 文件仓库 — 读写 / 缓存管理。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pandas as pd


def data_root() -> Path:
    from quant_data import get_quant_data_dir
    d = get_quant_data_dir()
    if d:
        return Path(d) / "data"
    return Path(__file__).resolve().parent.parent / "data"


def static_dir() -> Path:
    return data_root() / "static"


def daily_dir() -> Path:
    return data_root() / "daily"


def daily_basic_dir() -> Path:
    return data_root() / "daily_basic"


def moneyflow_dir() -> Path:
    return data_root() / "moneyflow"


def ths_member_dir() -> Path:
    return data_root() / "ths_member"


def per_stock_dir(api: str, ts_code: str) -> Path:
    return data_root() / "per_stock" / api / ts_code.replace(".", "_")


# ── 读 ──


def read_static(name: str) -> Optional[pd.DataFrame]:
    """读取静态数据，如 stock_basic, trade_cal, ths_index。"""
    p = static_dir() / f"{name}.parquet"
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        p.unlink(missing_ok=True)
        return None


# 全市场按日分区：行数过少视为被单股查询误覆盖
_MARKET_PARTITION_MIN_ROWS: dict[str, int] = {
    "daily": 2000,
    "moneyflow": 2000,
    "daily_basic": 2000,
}


def market_partition_min_rows(api_or_dir_name: str) -> int:
    name = Path(api_or_dir_name).name if "/" in str(api_or_dir_name) else str(api_or_dir_name)
    return _MARKET_PARTITION_MIN_ROWS.get(name, 50)


def date_partition_row_count(base_dir: Path, trade_date: str) -> int:
    p = base_dir / f"{trade_date}.parquet"
    if not p.exists():
        return 0
    try:
        return len(pd.read_parquet(p))
    except Exception:
        return 0


def is_healthy_market_partition(base_dir: Path, trade_date: str) -> bool:
    """全市场日分区是否完整（非单股误写的小文件）。"""
    min_rows = market_partition_min_rows(base_dir)
    if min_rows <= 50:
        return date_partition_row_count(base_dir, trade_date) > 0
    return date_partition_row_count(base_dir, trade_date) >= min_rows


def should_write_market_date_partition(
    api: str, base_dir: Path, trade_date: str, df: pd.DataFrame
) -> bool:
    """仅允许写入全市场级分区；拒绝单股小结果覆盖已有全市场缓存。"""
    if df is None or df.empty:
        return False
    min_rows = market_partition_min_rows(api)
    n = len(df)
    if n < min_rows:
        return False
    if not is_healthy_market_partition(base_dir, trade_date):
        return True
    return n >= min_rows


def read_date_partition(base_dir: Path, trade_date: str, ttl_sec: int = 21600) -> Optional[pd.DataFrame]:
    """读取按交易日分区的 Parquet，检查 TTL。"""
    p = base_dir / f"{trade_date}.parquet"
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > ttl_sec:
        return None
    try:
        df = pd.read_parquet(p)
        if not is_healthy_market_partition(base_dir, trade_date):
            return None
        return df
    except Exception:
        p.unlink(missing_ok=True)
        return None


def read_ths_member(index_code: str) -> Optional[pd.DataFrame]:
    p = ths_member_dir() / f"{index_code.replace('.', '_')}.parquet"
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        p.unlink(missing_ok=True)
        return None


def read_per_stock(api: str, ts_code: str, date: str) -> Optional[pd.DataFrame]:
    p = per_stock_dir(api, ts_code) / f"{date}.parquet"
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        p.unlink(missing_ok=True)
        return None


def read_per_stock_all(api: str, ts_code: str) -> Optional[pd.DataFrame]:
    """读取某只股票某个 API 的全部历史（拼接多个日期）。"""
    d = per_stock_dir(api, ts_code)
    if not d.exists():
        return None
    parts = sorted(d.glob("*.parquet"))
    if not parts:
        return None
    dfs = []
    for p in parts:
        try:
            dfs.append(pd.read_parquet(p))
        except Exception:
            p.unlink(missing_ok=True)  # 删除损坏的文件
    if not dfs:
        return None
    return pd.concat(dfs, ignore_index=True)


# ── 写 ──


def write_static(name: str, df: pd.DataFrame) -> Path:
    p = static_dir() / f"{name}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    return p


def write_date_partition(base_dir: Path, trade_date: str, df: pd.DataFrame) -> Path:
    p = base_dir / f"{trade_date}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    return p


def write_ths_member(index_code: str, df: pd.DataFrame) -> Path:
    p = ths_member_dir() / f"{index_code.replace('.', '_')}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    return p


def write_per_stock(api: str, ts_code: str, date: str, df: pd.DataFrame) -> Path:
    d = per_stock_dir(api, ts_code)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date}.parquet"
    df.to_parquet(p, index=False)
    return p


# ── 清理 ──


def clear_cache():
    """清空所有 Parquet 缓存（保留目录结构）。"""
    root = data_root()
    for f in root.rglob("*.parquet"):
        f.unlink()
