"""交易日工具。"""

from __future__ import annotations

from datetime import datetime


def latest_open_trade_date(pro, *, exchange: str = "SSE", end_date: str | None = None) -> str:
    """
    返回 ``exchange`` 交易所最近一次开市日 ``YYYYMMDD``。

    ``end_date`` 默认今天（本地日期）。
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")
    cal = pro.trade_cal(exchange=exchange, end_date=end_date, is_open="1")
    if cal is None or cal.empty:
        raise RuntimeError(f"trade_cal 无数据：exchange={exchange}, end_date={end_date}")
    return str(int(cal.cal_date.max()))
