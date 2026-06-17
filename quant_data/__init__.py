"""
quant_data — 统一数据层 for 选股系统。

按计划：四个项目通过本包读取本地 Parquet，透明回退 Tushare API。

用法：
    from quant_data import get_provider
    pro = get_provider(token="xxx")
    df = pro.daily(trade_date="20260519")
"""

from __future__ import annotations

import os
from typing import Optional

_QUANT_DATA_DIR = os.environ.get("QUANT_DATA_DIR", "")
_QUANT_CONCEPTS_PATH = os.environ.get("QUANT_CONCEPTS_PATH", "")

# 统一 daily_basic 字段列表，确保日期分区字段一致
DAILY_BASIC_FIELDS = "ts_code,trade_date,turnover_rate,turnover_rate_f,circ_mv,total_mv,pe,pe_ttm,pb,ps,ps_ttm"


def get_quant_data_dir() -> str:
    return _QUANT_DATA_DIR or ""


def get_quant_concepts_path() -> str:
    return _QUANT_CONCEPTS_PATH or ""


def get_provider(
    token: Optional[str] = None,
    data_dir: Optional[str] = None,
    concepts_path: Optional[str] = None,
):
    """获取 DataProvider 实例。

    优先级：
    1. 显式传入 data_dir
    2. 环境变量 QUANT_DATA_DIR
    3. None（直接走 Tushare 回退）
    """
    from quant_data.provider import DataProvider

    dd = data_dir or _QUANT_DATA_DIR or None
    cp = concepts_path or _QUANT_CONCEPTS_PATH or None
    return DataProvider(data_dir=dd, token=token, concepts_path=cp)
