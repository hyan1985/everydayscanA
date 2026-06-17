"""Tushare 请求节流（按接口文档的典型限额：积分档位 + 单接口特例）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

# 文档参考（以官方为准，会随平台调整）：
# - 5000 积分：stk_factor_pro 约 30 次/分钟 → 相邻调用间隔 ≥ 2s
# - 8000 积分以上：stk_factor_pro 约 500 次/分钟 → 约 0.13s
# - hm_detail：社区常见频控约 2 次/小时


Kind = Literal["general", "stk_factor_pro", "hm_detail"]


@dataclass
class TushareThrottle:
    """
    在**两次请求之间**插入等待，降低触发频控概率。

    - ``general``：两次任意请求的最小间隔（覆盖 daily / moneyflow / fina / cyq / ths_* 等）。
    - ``stk_factor_pro``：额外约束：两次 stk_factor 请求的最小间隔（5000 积分默认 2.05s）。
    - ``hm_detail``：额外约束：两次 hm_detail 的最小间隔（默认 1801s，配合约 2 次/小时频控；仍建议少用）。
    - ``extra_after``：每次请求完成后再多睡一会（手动加压）。
    """

    global_min_s: float = 0.13
    stk_factor_min_s: float = 2.05
    hm_detail_min_s: float = 1801.0
    extra_after_s: float = 0.0
    fast_stk_factor_min_s: float = 0.13

    _last_any: float | None = field(default=None, repr=False)
    _last_stk: float | None = field(default=None, repr=False)
    _last_hm: float | None = field(default=None, repr=False)

    @classmethod
    def for_points(cls, points: int, *, extra_after_s: float = 0.0) -> TushareThrottle:
        """按积分档位选择 stk_factor 间隔：>=8000 用快速档。"""
        t = cls(extra_after_s=extra_after_s)
        if points >= 8000:
            t.stk_factor_min_s = t.fast_stk_factor_min_s
        return t

    def pace_before(self, kind: Kind) -> None:
        """在发起请求前调用。"""
        now = time.monotonic()
        if kind == "hm_detail":
            if self._last_hm is not None:
                dt = self.hm_detail_min_s - (now - self._last_hm)
                if dt > 0:
                    time.sleep(dt)
                now = time.monotonic()
        if kind == "stk_factor_pro":
            if self._last_stk is not None:
                dt = self.stk_factor_min_s - (now - self._last_stk)
                if dt > 0:
                    time.sleep(dt)
                now = time.monotonic()
        if self._last_any is not None:
            dt = self.global_min_s - (now - self._last_any)
            if dt > 0:
                time.sleep(dt)

    def mark_after(self, kind: Kind) -> None:
        """在请求返回后调用（无论成功与否，用于节奏；失败同样占用频控窗口）。"""
        now = time.monotonic()
        self._last_any = now
        if kind == "stk_factor_pro":
            self._last_stk = now
        if kind == "hm_detail":
            self._last_hm = now
        if self.extra_after_s > 0:
            time.sleep(self.extra_after_s)
