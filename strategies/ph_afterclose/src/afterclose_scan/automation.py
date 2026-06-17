from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path

import pandas as pd

from .models import MarketSnapshot, SectorSnapshot, StockSnapshot
from .tushare_client import TushareClient


def _exclude_unavailable_boards(df: pd.DataFrame) -> pd.DataFrame:
    """仅排除北交所，保留沪深主板 + 创业板(300/301) + 科创板(688)"""
    out = df.copy()
    out = out[~out["ts_code"].astype(str).str.endswith(".BJ")]
    return out


def _stage_from_strength(avg_pct: float) -> str:
    if avg_pct >= 3.0:
        return "加速"
    if avg_pct >= 1.2:
        return "发酵"
    if avg_pct >= 0.0:
        return "分歧"
    return "反抽"


def _position_tag(pct_chg: float, turnover_rate: float) -> str:
    if pct_chg >= 9.5:
        return "一致后加速"
    if pct_chg >= 5.0:
        return "二板确认"
    if turnover_rate >= 2.0:
        return "低位首板"
    return "高位"


def _pick_sector_leaders(pool_all: pd.DataFrame) -> dict[str, dict[str, float | str]]:
    """每个板块唯一龙头：涨幅优先；并列时按成交额→换手→代码（日线可得的代理强度）。"""
    if pool_all.empty or "sector" not in pool_all.columns:
        return {}
    df = pool_all.copy()
    df["_pct"] = pd.to_numeric(df.get("pct_chg", 0), errors="coerce").fillna(0.0)
    # daily.amount 多为千元，排序只需相对大小一致即可
    df["_amt"] = pd.to_numeric(df.get("amount", 0), errors="coerce").fillna(0.0)
    df["_to"] = pd.to_numeric(df.get("turnover_rate", 0), errors="coerce").fillna(0.0)
    df["_code"] = df["ts_code"].astype(str)
    df = df.sort_values(
        by=["sector", "_pct", "_amt", "_to", "_code"],
        ascending=[True, False, False, False, True],
    )
    tops = df.groupby("sector", as_index=False).first()
    out: dict[str, dict[str, float | str]] = {}
    for _, r in tops.iterrows():
        sec = str(r["sector"])
        out[sec] = {"ts_code": str(r["ts_code"]), "pct_chg": float(r["_pct"])}
    return out


def _build_market_snapshot(daily_df: pd.DataFrame) -> MarketSnapshot:
    limit_up = (daily_df["pct_chg"] >= 9.5).sum()
    strong = (daily_df["pct_chg"] >= 5.0).sum()
    weak = (daily_df["pct_chg"] <= -5.0).sum()
    total = max(len(daily_df), 1)
    promotion = min(limit_up / max(strong, 1), 1.0)
    blowup = min(weak / total * 2.0, 0.6)
    max_board = 2 + min(limit_up // 20, 4)
    return MarketSnapshot(
        max_board_height=int(max_board),
        promotion_rate=float(promotion),
        blowup_rate=float(blowup),
        index_pct_chg=float(daily_df["pct_chg"].mean()),
        median_pct_chg=float(daily_df["pct_chg"].median()),
    )


def _build_sector_snapshots(merged_df: pd.DataFrame, top_n: int = 8) -> list[SectorSnapshot]:
    sector_df = (
        merged_df.groupby("sector", as_index=False)
        .agg(
            pct_chg=("pct_chg", "mean"),
            persistence_score=("turnover_rate", "mean"),
            leader_strength=("pct_chg", "max"),
            count=("ts_code", "count"),
        )
        .query("count >= 3")
    )
    if sector_df.empty:
        return []
    sector_df["persistence_score"] = (
        sector_df["persistence_score"].fillna(0).clip(lower=0, upper=10)
    )
    sector_df = sector_df.sort_values(
        by=["pct_chg", "leader_strength"], ascending=False
    ).head(top_n)
    items: list[SectorSnapshot] = []
    for _, row in sector_df.iterrows():
        items.append(
            SectorSnapshot(
                name=str(row["sector"]),
                pct_chg=round(float(row["pct_chg"]), 2),
                persistence_score=round(float(row["persistence_score"]), 1),
                stage=_stage_from_strength(float(row["pct_chg"])),
            )
        )
    return items


def _concept_sectors_ths(
    pro,
    merged: pd.DataFrame,
    last_trade_date: str,
    hot_sector_n: int,
    max_member_scan: int = 150,
) -> tuple[pd.DataFrame, list[SectorSnapshot], str]:
    """同花顺概念：ths_index(N) + 当日 ths_daily 涨跌幅 + ths_member 成分。"""
    merged_codes = set(merged["ts_code"].astype(str))
    idx = pro.ths_index(exchange="A", type="N")
    if idx is None or idx.empty:
        raise RuntimeError("ths_index 无返回（需积分权限：同花顺板块）")
    daily = pro.ths_daily(trade_date=last_trade_date, fields="ts_code,pct_change")
    if daily is None or daily.empty:
        daily = pro.ths_daily(
            start_date=last_trade_date,
            end_date=last_trade_date,
            fields="ts_code,pct_change",
        )
    if daily is None or daily.empty:
        raise RuntimeError("ths_daily 无当日板块指数行情")
    hot = idx.merge(daily, on="ts_code", how="inner")
    if hot.empty:
        raise RuntimeError("板块指数与指数列表无法对齐")
    hot = hot.sort_values("pct_change", ascending=False)
    selected: list[dict] = []
    scanned = 0
    for _, row in hot.iterrows():
        scanned += 1
        if scanned > max_member_scan:
            break
        memb = pro.ths_member(ts_code=row["ts_code"])
        if memb is None or memb.empty:
            continue
        intr = set(memb["con_code"].astype(str)) & merged_codes
        if len(intr) < 3:
            continue
        selected.append(
            {
                "ts_code": str(row["ts_code"]),
                "name": str(row["name"]),
                "index_pct": float(row["pct_change"]),
                "codes": intr,
            }
        )
        if len(selected) >= hot_sector_n:
            break
    if not selected:
        raise RuntimeError(
            "未筛出可用概念板块（成分股与当前可交易池交集不足 3 只，或权限/扫描上限）"
        )
    stock_to_sector: dict[str, str] = {}
    for meta in selected:
        for code in meta["codes"]:
            if code not in stock_to_sector:
                stock_to_sector[code] = meta["name"]
    out = merged.copy()
    out["sector"] = out["ts_code"].astype(str).map(stock_to_sector)
    out = out[out["sector"].notna()].copy()
    if out.empty:
        raise RuntimeError("概念映射后股票池为空")
    sectors: list[SectorSnapshot] = []
    for meta in selected:
        name = meta["name"]
        sub = out[out["sector"] == name]
        if sub.empty:
            continue
        pers = float(sub["turnover_rate"].fillna(0).mean())
        pers = max(0.0, min(10.0, pers))
        ip = float(meta["index_pct"])
        sectors.append(
            SectorSnapshot(
                name=name,
                pct_chg=round(ip, 2),
                persistence_score=round(pers, 1),
                stage=_stage_from_strength(ip),
            )
        )
    note = (
        "板块口径：同花顺概念（ths_index type=N）；"
        "列表中的「板块涨幅」为当日板块指数涨跌幅 pct_change；"
        "龙头：成分内在当前池中涨幅最高者；若多只并列（如均涨停），"
        "再按当日成交额→换手率→代码次序取唯一代表（日线代理，非封单先后）。"
    )
    return out, sectors, note


def _build_stock_snapshots(
    merged_df: pd.DataFrame,
    selected_sectors: set[str],
    top_n: int = 30,
    max_per_sector: int = 4,
) -> list[StockSnapshot]:
    pool_all = merged_df[merged_df["sector"].isin(selected_sectors)].copy()
    if pool_all.empty:
        pool_all = merged_df.copy()
    leaders = _pick_sector_leaders(pool_all)

    pool = pool_all.copy()
    # daily_basic 可能因权限/限流导致字段缺失；缺失时用中性默认值保证流程不中断
    if "volume_ratio" not in pool.columns:
        pool["volume_ratio"] = 1.0
    else:
        pool["volume_ratio"] = pool["volume_ratio"].fillna(1.0)
    if "turnover_rate" not in pool.columns:
        pool["turnover_rate"] = 0.0
    else:
        pool["turnover_rate"] = pool["turnover_rate"].fillna(0.0)
    pool["score_proxy"] = (
        pool["pct_chg"] * 0.55 + pool["turnover_rate"] * 0.25 + pool["volume_ratio"] * 0.20
    )
    pool = pool.sort_values(by="score_proxy", ascending=False)
    pool = (
        pool.groupby("sector", group_keys=False)
        .head(max_per_sector)
        .head(top_n)
    )

    sector_rank_map: dict[tuple[str, str], int] = {}
    for sector, sec_df in pool.sort_values(by="score_proxy", ascending=False).groupby("sector"):
        for rank, (_, r) in enumerate(sec_df.iterrows(), start=1):
            sector_rank_map[(str(sector), str(r["ts_code"]))] = rank

    items: list[StockSnapshot] = []
    for _, row in pool.iterrows():
        turnover_rate = float(row.get("turnover_rate", 0.0))
        pct_chg = float(row.get("pct_chg", 0.0))
        pct_chg = max(-20.0, min(20.0, pct_chg))
        amount = float(row.get("amount", 0.0)) * 1000.0
        volume_ratio = float(row.get("volume_ratio", 1.0))
        sector_name = str(row.get("sector") or "其他")
        code = str(row["ts_code"])

        leader_info = leaders.get(sector_name, {})
        leader_code = str(leader_info.get("ts_code", ""))
        leader_pct = float(leader_info.get("pct_chg", 0.0))
        is_limit_up = pct_chg >= 9.8
        has_limit_up_leader = leader_pct >= 9.5

        items.append(
            StockSnapshot(
                name=str(row.get("name") or code),
                code=code,
                sector=sector_name,
                pct_chg=round(pct_chg, 2),
                turnover_rate=round(turnover_rate, 2),
                volume_ratio=round(volume_ratio, 2),
                amount=round(amount, 2),
                float_mkt_cap_billion=round(float(row.get("float_mv", 0.0)) / 10000.0, 2),
                position_tag=_position_tag(pct_chg, turnover_rate),
                is_sector_leader=(leader_code == code),
                has_catalyst=bool(volume_ratio >= 1.8),
                popularity_score=round(min(10.0, max(2.0, turnover_rate * 0.8 + 4.0)), 1),
                is_limit_up=is_limit_up,
                has_limit_up_leader_in_sector=has_limit_up_leader,
                follow_rank_in_sector=sector_rank_map.get((sector_name, code), 99),
                leader_pct_chg=round(leader_pct, 2),
            )
        )
    return items


def build_auto_input(
    output_path: Path,
    top_n: int = 20,
    hot_sector_n: int = 3,
    per_sector_n: int = 4,
    target_trade_date: str | None = None,
    sector_mode: str = "ths_concept",
) -> Path:
    pro = TushareClient.from_secure_config().pro()

    end = datetime.now().date()
    if target_trade_date:
        start = datetime.strptime(str(target_trade_date), "%Y%m%d").date() - timedelta(days=5)
    else:
        start = end - timedelta(days=25)
    cal = pro.trade_cal(
        exchange="",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        is_open=1,
    )
    if cal is None or cal.empty:
        raise RuntimeError("无法获取交易日历，请检查 Tushare 权限或网络。")
    cal = cal.sort_values("cal_date")
    if target_trade_date:
        target_trade_date = str(target_trade_date)
        cal_dates = set(cal["cal_date"].astype(str).str.split(".").str[0].tolist())
        if target_trade_date not in cal_dates:
            raise RuntimeError(f"指定 trade_date={target_trade_date} 不是可交易日。")
        last_trade_date = target_trade_date
    else:
        last_trade_date = str(cal.iloc[-1]["cal_date"]).split(".")[0]

    # 非交易日自动回退到最近交易日（而非抛异常中断流水线）
    today_ymd = datetime.now().strftime("%Y%m%d")
    if last_trade_date != today_ymd:
        print(
            f"[盘后扫描] 今天 {today_ymd} 非交易日，"
            f"自动使用最近交易日 {last_trade_date} 的数据。"
        )

    daily = pro.daily(trade_date=last_trade_date, fields="ts_code,open,close,pct_chg,vol,amount")
    daily_basic = pro.daily_basic(
        trade_date=last_trade_date,
        fields="ts_code,turnover_rate,volume_ratio,float_mv",
    )
    stock_basic = pro.stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,name,industry,market,list_date",
    )
    if daily is None or daily.empty:
        raise RuntimeError(
            f"未拉到 {last_trade_date} 行情数据，"
            f"建议在收盘后或确认权限后重试。"
        )

    if daily_basic is None or daily_basic.empty:
        daily_basic = pd.DataFrame(columns=["ts_code", "turnover_rate", "volume_ratio", "float_mv"])
    if stock_basic is None or stock_basic.empty:
        stock_basic = pd.DataFrame(columns=["ts_code", "name", "industry", "market", "list_date"])
    merged = daily.merge(daily_basic, on="ts_code", how="left").merge(
        stock_basic, on="ts_code", how="left"
    )
    merged = _exclude_unavailable_boards(merged)
    if merged.empty:
        raise RuntimeError("排除北交所后无可用股票，请检查数据源。")
    merged["industry"] = merged["industry"].fillna("其他")

    market = _build_market_snapshot(merged)

    mode_used = sector_mode
    sector_note = ""
    try:
        if sector_mode == "ths_concept":
            merged_use, sectors, sector_note = _concept_sectors_ths(
                pro, merged, last_trade_date, hot_sector_n=max(1, hot_sector_n)
            )
        else:
            merged_use = merged.copy()
            merged_use["sector"] = merged_use["industry"]
            sectors = _build_sector_snapshots(
                merged_use, top_n=max(3, hot_sector_n)
            )
    except Exception as exc:
        merged_use = merged.copy()
        merged_use["sector"] = merged_use["industry"]
        sectors = _build_sector_snapshots(merged_use, top_n=max(3, hot_sector_n))
        mode_used = "industry"
        sector_note = f"概念板块失败（{exc}），已回退行业口径。"

    selected = {s.name for s in sectors[: max(1, hot_sector_n)]}
    cap_n = min(top_n, max(1, hot_sector_n) * max(1, per_sector_n))
    stocks = _build_stock_snapshots(
        merged_use,
        selected_sectors=selected,
        top_n=cap_n,
        max_per_sector=max(1, per_sector_n),
    )

    data = {
        "meta": {
            "source": "tushare_afterclose",
            "trade_date": last_trade_date,
            "sector_mode": mode_used,
            "sector_note": sector_note,
            "note": (
                "盘后扫描，基于当日收盘数据生成次日跟随计划。"
                + (" " + sector_note if sector_note else "")
            ),
        },
        "market": asdict(market),
        "sectors": [asdict(s) for s in sectors],
        "stocks": [asdict(s) for s in stocks],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path
