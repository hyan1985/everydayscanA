from __future__ import annotations

from dataclasses import dataclass

from .config import get_tushare_token


@dataclass
class TushareClient:
    token: str

    @classmethod
    def from_secure_config(cls) -> "TushareClient":
        return cls(token=get_tushare_token())

    def pro(self):
        """返回 DataProvider，优先本地 Parquet，回退 Tushare。"""
        from quant_data import get_provider

        return get_provider(token=self.token)

    def check_connection(self) -> bool:
        try:
            df = self.pro().trade_cal(exchange="", limit=1)
            return df is not None and not df.empty
        except Exception:
            return False

