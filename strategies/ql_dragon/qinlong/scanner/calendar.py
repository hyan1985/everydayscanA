"""交易日工具。"""

from __future__ import annotations

from datetime import datetime, timedelta


def _today_data_ready() -> bool:
    """当天行情数据是否已生成：A股 15:00 收盘，数据一般在收盘后一段才可获取。"""
    now = datetime.now()
    return (now.hour, now.minute) >= (15, 30)


def latest_open_trade_date(pro, *, exchange: str = "SSE", end_date: str | None = None) -> str:
    """
    返回 ``exchange`` 交易所最近一次已收盘的开市日 ``YYYYMMDD``。

    ``end_date`` 默认今天（本地日期）。若当天尚未收盘（盘前/盘中触发），
    自动回退到上一个交易日，避免用未生成数据的今天作扫描日。
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")
    if end_date >= today and not _today_data_ready():
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    cal = pro.trade_cal(exchange=exchange, end_date=end_date, is_open="1")
    if cal is None or cal.empty:
        raise RuntimeError(f"trade_cal 无数据：exchange={exchange}, end_date={end_date}")
    return str(int(cal.cal_date.max()))
