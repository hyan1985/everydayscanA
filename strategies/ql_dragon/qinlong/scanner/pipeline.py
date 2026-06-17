"""造龙风格扫描主流程。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from qinlong.scanner import candidates as cand_mod
from qinlong.scanner import metrics
from qinlong.scanner.calendar import latest_open_trade_date
from qinlong.scanner.throttle import TushareThrottle


def _is_st_name(name: str) -> bool:
    n = str(name or "")
    return "ST" in n or "退" in n


def _load_st_map(pro, throttle: TushareThrottle) -> set[str]:
    """名称含 ST 或退市的代码。"""
    throttle.pace_before("general")
    try:
        basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
    finally:
        throttle.mark_after("general")
    if basic is None or basic.empty:
        return set()
    bad = basic[basic["name"].apply(_is_st_name)]
    return set(bad["ts_code"].astype(str).tolist())


def _load_basic_lookup(pro, throttle: TushareThrottle) -> pd.DataFrame:
    throttle.pace_before("general")
    try:
        basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
    finally:
        throttle.mark_after("general")
    if basic is None:
        return pd.DataFrame(columns=["ts_code", "name", "industry"])
    return basic


class DragonScanner:
    """
    以「题材热度 + 资金行为 + 技术突破 + 筹码/基本面」组合识别 A 股常见造龙结构。

    仅作研究辅助，不构成投资建议。
    """

    def __init__(self, pro, trade_date: str | None = None, *, weights: dict[str, float] | None = None):
        self.pro = pro
        self.trade_date = trade_date
        self.weights = weights or metrics.default_weights()

    def _resolve_trade_date(self) -> str:
        if self.trade_date:
            return self.trade_date
        return latest_open_trade_date(self.pro)

    def run(
        self,
        *,
        top_concepts: int = 12,
        max_analyze: int = 80,
        points_tier: int = 5000,
        extra_sleep: float = 0.0,
        throttle: TushareThrottle | None = None,
        skip_fina: bool = False,
        skip_chip: bool = False,
        skip_hm_detail: bool = True,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        d = self._resolve_trade_date()
        debug: dict[str, Any] = {"trade_date": d}

        th = throttle or TushareThrottle.for_points(points_tier, extra_after_s=extra_sleep)
        debug["throttle"] = {
            "global_min_s": th.global_min_s,
            "stk_factor_min_s": th.stk_factor_min_s,
            "hm_detail_min_s": th.hm_detail_min_s,
            "extra_after_s": th.extra_after_s,
            "points_tier": points_tier,
        }

        st_set = _load_st_map(self.pro, th)
        basic_lookup = _load_basic_lookup(self.pro, th).set_index("ts_code")

        meta, cdebug = cand_mod.collect_candidates(
            self.pro,
            d,
            th,
            top_concepts=top_concepts,
            skip_hm_detail=skip_hm_detail,
        )
        debug["candidates"] = cdebug

        if meta is None or meta.empty:
            return pd.DataFrame(), debug

        meta = meta[~meta["ts_code"].isin(st_set)].copy()

        hm_series = pd.to_numeric(meta["hm_net_amount"], errors="coerce").fillna(0.0)
        hm_abs_max = float(hm_series.abs().max() or 0.0)
        if hm_abs_max <= 0:
            hm_abs_max = 1.0

        meta = meta.head(max_analyze)

        factor_fields = (
            "ts_code,trade_date,macd_dif_qfq,macd_dea_qfq,macd_qfq,"
            "ema_qfq_5,ema_qfq_10,ema_qfq_20,ema_qfq_60,kdj_k_qfq,kdj_d_qfq"
        )

        bundle: list[dict[str, Any]] = []
        mf_nets: list[float] = []

        for _, mrow in meta.iterrows():
            ts = str(mrow["ts_code"])
            start = metrics.offset_trade_calendar_days(d, delta_days=-420)

            daily = None
            sf = None
            mf_net = None
            fi_row = None
            cyq_wr = None
            db_row = None

            try:
                th.pace_before("general")
                try:
                    daily = self.pro.daily(ts_code=ts, start_date=start, end_date=d)
                finally:
                    th.mark_after("general")
            except Exception:
                daily = None

            try:
                th.pace_before("stk_factor_pro")
                try:
                    sf = self.pro.stk_factor_pro(ts_code=ts, trade_date=d, fields=factor_fields)
                finally:
                    th.mark_after("stk_factor_pro")
            except Exception:
                sf = None

            try:
                th.pace_before("general")
                try:
                    mf = self.pro.moneyflow_dc(ts_code=ts, trade_date=d)
                finally:
                    th.mark_after("general")
                if mf is not None and not mf.empty:
                    mf_net = float(mf.iloc[0].get("net_amount") or 0.0)
                    mf_nets.append(abs(mf_net))
            except Exception:
                mf_net = None

            try:
                th.pace_before("general")
                try:
                    db = self.pro.daily_basic(ts_code=ts, trade_date=d, fields="ts_code,trade_date,turnover_rate,turnover_rate_f,circ_mv,total_mv")
                finally:
                    th.mark_after("general")
                if db is not None and not db.empty:
                    if "ts_code" in db.columns:
                        hit = db[db["ts_code"].astype(str) == str(ts)]
                        db_row = hit.iloc[0] if not hit.empty else None
                    else:
                        db_row = db.iloc[0] if len(db) == 1 else None
                else:
                    db_row = None
            except Exception:
                db_row = None

            if not skip_fina:
                try:
                    th.pace_before("general")
                    try:
                        fi = self.pro.fina_indicator(
                            ts_code=ts,
                            fields="ts_code,end_date,ann_date,revenue_yoy,profit_dedt_yoy,roe",
                        )
                    finally:
                        th.mark_after("general")
                    if fi is not None and not fi.empty:
                        fi = fi.sort_values(["end_date", "ann_date"], ascending=False)
                        fi_row = fi.iloc[0]
                except Exception:
                    fi_row = None

            if not skip_chip:
                try:
                    th.pace_before("general")
                    try:
                        cyq = self.pro.cyq_perf(ts_code=ts, trade_date=d)
                    finally:
                        th.mark_after("general")
                    if cyq is not None and not cyq.empty:
                        cyq_wr = float(cyq.iloc[0].get("winner_rate"))
                except Exception:
                    cyq_wr = None

            row_basic = basic_lookup.loc[ts] if ts in basic_lookup.index else None
            name = str(row_basic["name"]) if row_basic is not None else ts
            industry = str(row_basic["industry"]) if row_basic is not None else ""

            bundle.append(
                {
                    "mrow": mrow,
                    "daily": daily,
                    "sf": sf,
                    "mf_net": mf_net,
                    "fi_row": fi_row,
                    "cyq_wr": cyq_wr,
                    "db_row": db_row,
                    "name": name,
                    "industry": industry,
                }
            )

        mf_abs_max = max(mf_nets) if mf_nets else 1.0
        if mf_abs_max <= 0:
            mf_abs_max = 1.0

        rows_out: list[dict[str, Any]] = []

        for pack in bundle:
            mrow = pack["mrow"]
            ts = str(mrow["ts_code"])
            daily = pack["daily"]
            sf = pack["sf"]
            mf_net = pack["mf_net"]
            fi_row = pack["fi_row"]
            cyq_wr = pack["cyq_wr"]
            db_row = pack.get("db_row")

            s_theme = metrics.score_theme_heat(
                float(mrow["best_concept_rank"]) if pd.notna(mrow["best_concept_rank"]) else None,
                from_limit=bool(mrow["from_limit_list"]),
            )
            hm_net = None if pd.isna(mrow.get("hm_net_amount")) else float(mrow["hm_net_amount"])
            s_news = metrics.score_news_proxy(
                hm_net,
                hm_abs_max=hm_abs_max,
                mf_net=mf_net,
                mf_abs_max=mf_abs_max,
            )

            s_daily, dbg_daily = metrics.score_technical_from_daily(daily) if daily is not None else (0.0, {})
            s_fac = 0.0
            dbg_fac: dict[str, float] = {}
            if sf is not None and not sf.empty:
                s_fac, dbg_fac = metrics.score_technical_from_factor(sf.iloc[0])

            s_tech = metrics.merge_technical_scores(s_daily, s_fac, w_daily=0.5)

            circ_mv = None
            turnover_rate = None
            if db_row is not None:
                if pd.notna(db_row.get("circ_mv")):
                    circ_mv = float(db_row["circ_mv"])
                elif pd.notna(db_row.get("total_mv")):
                    # 本地 daily_basic 分区可能缺 circ_mv，用 total_mv 近似流通市值打分
                    circ_mv = float(db_row["total_mv"])
                if pd.notna(db_row.get("turnover_rate")):
                    turnover_rate = float(db_row["turnover_rate"])
            s_cap_turnover, dbg_ct = metrics.score_market_cap_and_turnover(circ_mv, turnover_rate)

            if skip_fina:
                s_fina = 5.0
            elif fi_row is not None:
                s_fina = metrics.score_fundamental_row(fi_row)
            else:
                s_fina = 5.0

            if skip_chip:
                s_chip = 5.0
            else:
                s_chip = metrics.score_chip_winner_rate(cyq_wr)

            parts = {
                "theme": s_theme,
                "news": s_news,
                "technical": s_tech,
                "cap_turnover": s_cap_turnover,
                "fundamental": s_fina,
                "chip": s_chip,
            }
            total = metrics.composite_score(parts, self.weights)

            rows_out.append(
                {
                    "trade_date": d,
                    "ts_code": ts,
                    "name": pack["name"],
                    "industry": pack["industry"],
                    "score": round(total, 2),
                    "s_theme": round(s_theme, 2),
                    "s_news": round(s_news, 2),
                    "s_technical": round(s_tech, 2),
                    "s_cap_turnover": round(s_cap_turnover, 2),
                    "s_fundamental": round(s_fina, 2),
                    "s_chip": round(s_chip, 2),
                    "best_concept_rank": mrow.get("best_concept_rank"),
                    "concept_tags": mrow.get("concept_tags"),
                    "from_limit_list": mrow.get("from_limit_list"),
                    "from_hm_detail": mrow.get("from_hm_detail"),
                    "hm_net_amount": mrow.get("hm_net_amount"),
                    "mf_net_amount": mf_net,
                    "close_to_prev_high": dbg_daily.get("close_to_prev_high"),
                    "vol_to_ma5": dbg_daily.get("vol_to_ma5"),
                    "limit_up_days": dbg_daily.get("limit_up_days"),
                    "close": dbg_daily.get("close"),
                    "ema_bull": dbg_fac.get("ema_bull"),
                    "macd_ok": dbg_fac.get("macd_ok"),
                    "kdj_passivated": dbg_fac.get("kdj_passivated"),
                    "ema_5": dbg_fac.get("ema_5"),
                    "circ_mv_yi": dbg_ct.get("circ_mv_yi"),
                    "turnover_rate": dbg_ct.get("turnover_rate"),
                }
            )

        out = pd.DataFrame(rows_out)
        if not out.empty:
            out = out.sort_values("score", ascending=False).reset_index(drop=True)
        debug["rows"] = len(out)
        return out, debug
