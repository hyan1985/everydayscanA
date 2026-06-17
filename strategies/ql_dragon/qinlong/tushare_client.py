"""统一 DataProvider 入口。

优先读本地 Parquet 缓存，未命中再回退 Tushare API。
独立运行时（无 QUANT_DATA_DIR）直接回退原生 tushare pro_api。
"""

from __future__ import annotations

from qinlong.secrets import get_tushare_token


def get_pro_api(timeout: int | float = 30):
    """
    返回 DataProvider 实例，兼容 ``tushare.pro_api()`` 接口。

    Args:
        timeout: 请求超时秒数。
    """
    from quant_data import get_provider

    token = get_tushare_token()
    return get_provider(token=token)
