"""统一预拉取 — 在 run_all.sh 前置执行，把当日数据拉入 Parquet。

用法：
    python3 -m quant_data.fetcher --date 20260519 --lookback 60

输出：将每日数据写入统一输出/data/ 目录下 Parquet 文件。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import tushare as ts

from quant_data import storage as s
from quant_data.concepts import load_concepts

_INTERVAL = 0.15   # 常规 Tushare 限流间隔（日常增量用，6次/秒）
_FILL_INTERVAL = 0.8  # 后台填充用间隔（保守，≤ 75次/分钟，避免惩罚）


def _get_pro(token: str = ""):
    return ts.pro_api(token or os.environ.get("TUSHARE_TOKEN", ""))


def _trade_cal_max_date(cal: pd.DataFrame) -> str:
    if cal is None or cal.empty:
        return ""
    return str(cal["cal_date"].astype(str).max())


def _trade_cal_last_open(cal: pd.DataFrame) -> str:
    if cal is None or cal.empty or "is_open" not in cal.columns:
        return ""
    opens = cal.loc[cal["is_open"].astype(int) == 1, "cal_date"].astype(str)
    return str(opens.max()) if not opens.empty else ""


def trade_cal_needs_refresh(cal: Optional[pd.DataFrame], today: Optional[str] = None) -> bool:
    """日历是否需要刷新：表未覆盖到今天，或最近开市日早于今天。"""
    today = today or datetime.now().strftime("%Y%m%d")
    if cal is None or cal.empty:
        return True
    if _trade_cal_max_date(cal) < today:
        return True
    last_open = _trade_cal_last_open(cal)
    return (not last_open) or last_open < today


def ensure_trade_cal_current(pro, quiet: bool = False) -> pd.DataFrame:
    """保证 trade_cal 静态表覆盖到今天（run_all 预拉取 / 聚合前均应调用）。"""
    today = datetime.now().strftime("%Y%m%d")
    cached = s.read_static("trade_cal")
    if not trade_cal_needs_refresh(cached, today):
        if not quiet:
            print(
                f"  [SKIP] trade_cal 已最新 "
                f"(最近开市日 {_trade_cal_last_open(cached)}, max={_trade_cal_max_date(cached)})"
            )
        return cached if cached is not None else pd.DataFrame()
    if cached is not None and not cached.empty:
        print(
            f"  [REFRESH] trade_cal "
            f"(最近开市日 {_trade_cal_last_open(cached)} → 拉取至 {today})"
        )
    else:
        print(f"  [FETCH] trade_cal (至 {today}) ...", end="", flush=True)
    time.sleep(_INTERVAL)
    df = pro.trade_cal(exchange="SSE", start_date="20000101", end_date=today)
    if df is not None and not df.empty:
        s.write_static("trade_cal", df)
        if cached is None or cached.empty:
            print(f"{len(df)} rows (开市至 {_trade_cal_last_open(df)})")
        else:
            print(f"  → {len(df)} rows (最近开市日 {_trade_cal_last_open(df)})")
        return df
    print("empty")
    return pd.DataFrame()


def fetch_static(pro, config: dict) -> dict:
    """拉取低频静态数据（频繁调用不划算的）。"""
    fetched = {}
    today = datetime.now().strftime("%Y%m%d")

    # trade_cal：run_all 每次预拉取都会走 ensure_trade_cal_current（见 run_fetch 入口）
    cal_df = ensure_trade_cal_current(pro)
    if cal_df is not None and not cal_df.empty:
        fetched["trade_cal"] = cal_df

    cached_basic = s.read_static("stock_basic")
    if cached_basic is not None and not cached_basic.empty:
        print(f"  [SKIP] stock_basic 已缓存 ({len(cached_basic)} rows)")
    else:
        time.sleep(_INTERVAL)
        print("  [FETCH] stock_basic ... ", end="", flush=True)
        df = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,name,industry,list_date,market,is_hs",
        )
        if df is not None and not df.empty:
            s.write_static("stock_basic", df)
            print(f"{len(df)} rows")
            fetched["stock_basic"] = df
        else:
            print("empty")
    return fetched


def fetch_ths_index(pro) -> pd.DataFrame:
    """拉取同花顺概念/行业指数列表。"""
    cached = s.read_static("ths_index")
    if cached is not None and not cached.empty:
        print(f"  [SKIP] ths_index 已缓存 ({len(cached)} rows)")
        return cached
    time.sleep(_INTERVAL)
    print("  [FETCH] ths_index ... ", end="", flush=True)
    df = pro.ths_index()
    if df is not None and not df.empty:
        s.write_static("ths_index", df)
        print(f"{len(df)} rows")
    else:
        print("empty")
    return df if df is not None else pd.DataFrame()


def fetch_ths_members(pro, themes: list[str], keywords: list[str]) -> int:
    """根据概念别名词搜索 THS 指数，拉取成分股。"""
    idx = s.read_static("ths_index")
    if idx is None or idx.empty:
        idx = fetch_ths_index(pro)

    # 找匹配的指数
    matched_indices = set()
    for kw in keywords:
        if not kw:
            continue
        hits = idx[idx["name"].str.contains(kw, na=False, regex=False)]
        for _, r in hits.iterrows():
            matched_indices.add(r["ts_code"])

    total = 0
    for code in sorted(matched_indices):
        cached = s.read_ths_member(code)
        if cached is not None and not cached.empty:
            total += len(cached)
            continue
        time.sleep(_INTERVAL)
        method = pro.ths_member
        df = method(ts_code=code)
        if df is not None and not df.empty:
            s.write_ths_member(code, df)
            total += len(df)
    print(f"  [DONE] ths_member: {len(matched_indices)} indices, {total} total rows")
    return total


def fetch_date_partition(pro, api: str, date: str, fields: Optional[str] = None,
                         extra_kw: Optional[dict] = None,
                         is_today: bool = False) -> int:
    """拉取按日期分区的数据。

    Args:
        is_today: 是否为当日数据。当日数据用短 TTL（保证最新），历史数据永久缓存。
    """
    base_dir = _date_base_dir(api)
    ttl = 3600 if is_today else 86400 * 365  # 当日1小时, 历史永久(365天)
    cached = s.read_date_partition(base_dir, date, ttl_sec=ttl)
    if cached is not None and not cached.empty:
        return len(cached)

    time.sleep(_INTERVAL)
    print(f"  [FETCH] {api}({date}) ... ", end="", flush=True)
    kwargs = {"trade_date": date}
    if fields:
        kwargs["fields"] = fields
    if extra_kw:
        kwargs.update(extra_kw)
    method = getattr(pro, api)
    df = method(**kwargs)
    if df is not None and not df.empty:
        s.write_date_partition(base_dir, date, df)
        print(f"{len(df)} rows")
        return len(df)
    print("empty")
    return 0


def fetch_stock_basic_for_date(pro, date: str) -> pd.DataFrame:
    """拉取当日所有可交易股票的基础信息。"""
    basic = s.read_static("stock_basic")
    if basic is None or basic.empty:
        basic = fetch_static(pro, {})
        basic = s.read_static("stock_basic")
    return basic if basic is not None else pd.DataFrame()


def _latest_open_date(pro, end: str) -> str:
    cal = ensure_trade_cal_current(pro, quiet=True)
    if cal is not None and not cal.empty:
        mask = (cal["cal_date"].astype(str) <= end) & (
            cal["is_open"].astype(int) == 1
        )
        open_dates = cal.loc[mask, "cal_date"].astype(str)
        if not open_dates.empty:
            return str(open_dates.max())
    return end


def ensure_recent_market_partitions(
    pro,
    data_dir: Optional[Path] = None,
    lookback_open_days: int = 5,
) -> str:
    """刷新交易日历并补齐最近 N 个交易日的 daily/moneyflow 分区。返回最近交易日。"""
    today = datetime.now().strftime("%Y%m%d")
    ensure_trade_cal_current(pro)
    last_open = _latest_open_date(pro, today)
    root = data_dir or s.data_root()
    cal = s.read_static("trade_cal")
    if cal is None or cal.empty:
        return last_open
    open_dates = sorted(
        cal[(cal["cal_date"].astype(str) <= today) & (cal["is_open"].astype(int) == 1)][
            "cal_date"
        ].astype(str).tolist()
    )[-lookback_open_days:]
    date_apis = [
        ("daily", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"),
        (
            "moneyflow",
            "ts_code,trade_date,buy_sm_vol,sell_sm_vol,buy_md_vol,sell_md_vol,"
            "buy_lg_vol,sell_lg_vol,buy_elg_vol,sell_elg_vol,net_mf_vol,"
            "buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
            "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount",
        ),
    ]
    for td in open_dates:
        for api, fields in date_apis:
            path = root / api / f"{td}.parquet"
            if path.exists() and s.is_healthy_market_partition(root / api, td):
                continue
            if path.exists():
                print(f"  [重拉] {api}/{td}（分区不完整，仅 {s.date_partition_row_count(root / api, td)} 行）", flush=True)
                path.unlink(missing_ok=True)
            time.sleep(_INTERVAL)
            print(f"  [补拉] {api}/{td} ...", flush=True)
            df = getattr(pro, api)(trade_date=td, fields=fields)
            if df is not None and not df.empty:
                s.write_date_partition(root / api, td, df)
                print(f"    {len(df)} rows")
    return last_open


def run_fetch(
    trade_date: Optional[str] = None,
    lookback: int = 60,
    token: str = "",
):
    """主入口。"""
    pro = _get_pro(token)
    today = datetime.now().strftime("%Y%m%d")

    if not trade_date:
        trade_date = _latest_open_date(pro, today)
    print(f"交易日: {trade_date}")
    print(f"回看天数: {lookback}")

    # ── 0. 交易日历（run_all 步骤 0 必经，过期则自动刷新）──
    print("\n[0/4] 交易日历")
    ensure_trade_cal_current(pro)

    # ── 1. 静态数据 ──
    print("\n[1/4] 静态数据")
    concepts = load_concepts()
    fetch_static(pro, {})
    fetch_ths_index(pro)

    # ── 2. 概念成分 ──
    print("\n[2/4] 概念成分股")
    keywords = concepts.get_all_keywords()
    fetch_ths_members(pro, concepts.get_themes(), keywords)

    # ── 3. 每日市场数据 ──
    print(f"\n[3/4] 每日数据（交易日 {trade_date}）")
    # 主交易日 — is_today=True 确保数据最新
    date_apis = [
        ("daily", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"),
        ("daily_basic", "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,total_mv,circ_mv"),
        ("moneyflow", "ts_code,trade_date,buy_sm_vol,sell_sm_vol,buy_md_vol,sell_md_vol,buy_lg_vol,sell_lg_vol,buy_elg_vol,sell_elg_vol,net_mf_vol,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount"),
    ]
    for api, fields in date_apis:
        fetch_date_partition(pro, api, trade_date, fields=fields, is_today=(trade_date == today))

    # 回看交易日 — is_today=False 永久缓存，只需每日拉一次
    cal = s.read_static("trade_cal")
    if cal is not None and not cal.empty:
        mask = (cal["cal_date"].astype(str) <= trade_date) & (
            cal["is_open"].astype(int) == 1
        )
        open_dates = sorted(cal.loc[mask, "cal_date"].astype(str).tolist())[-lookback:]
        for d in open_dates:
            if d == trade_date:
                continue
            for api, fields in date_apis:
                fetch_date_partition(pro, api, d, fields=fields, is_today=False)

    # ── 4. 候选股 stk_factor_pro + 个股数据预拉取 ──
    #     日常走 "涨停+热榜前20" 快速通道（~2000只，约 8 分钟）
    #     --fill-per-stock 后台任务已覆盖全量板块成分股（4128只）的 daily 历史缓存
    print(f"\n[4/5] 候选股 stk_factor_pro — 逐只拉取当日因子（涨停+热榜板块）")
    candidate_codes = _fetch_daily_candidates(pro, trade_date)
    if candidate_codes:
        print(f"\n[4.5/6] 个股预拉取 — 为 {len(candidate_codes)} 只候选股拉取 per-stock 数据...")
        _prefetch_per_stock_data(pro, candidate_codes, trade_date, lookback)

    # ── 5. THS 日线 ──
    print(f"\n[5/6] THS 日线")
    fetch_date_partition(pro, "ths_daily", trade_date,
                         fields="ts_code,trade_date,pct_change,close,amount,vol",
                         is_today=(trade_date == today))

    # ── 6. 概念/行业指数日线（已通过 ths_daily 日期分区拉取，不再逐指数拉取）──
    print(f"\n[6/6] 跳过逐指数 THS 日线（已在步骤 5 中拉取全量日期分区）")
    print(f"  [SKIP] 盘后扫描追随、aggregate.py 均使用 ths_daily/ 日期分区文件")

    print(f"\n✅ 预拉取完成（{trade_date}，lookback={lookback}天）")


def _prefetch_per_stock_data(pro, codes: set[str], trade_date: str, lookback: int,
                              interval: float = _INTERVAL):
    """按个股预拉取各策略需要的 per-stock 数据，避免运行时回退 Tushare。

    覆盖：
    - stk_factor_pro（擒龙猎手用到）
    - moneyflow_dc（擒龙猎手 + 量化选股用到）
    - per-stock daily 历史（擒龙猎手 pipeline.py 里 pro.daily(ts_code=..., start_date=...) 会用到）
    - per-stock daily_basic（擒龙猎手用到）

    Args:
        interval: API 间隔（日常 0.15s；后台填充 0.8s，避免限流惩罚）
    """
    from quant_data import storage as s
    from quant_data.concepts import load_concepts

    # 计算回溯 starting date
    cal = s.read_static("trade_cal")
    start_date = ""
    if cal is not None and not cal.empty:
        open_dates = sorted(cal[cal["cal_date"].astype(str) <= trade_date]["cal_date"].astype(str).tolist())
        if len(open_dates) >= lookback:
            start_date = open_dates[-lookback]
        else:
            start_date = open_dates[0] if open_dates else ""

    if not start_date:
        return

    stk_factor_fields = (
        "ts_code,trade_date,macd_dif_qfq,macd_dea_qfq,macd_qfq,"
        "ema_qfq_5,ema_qfq_10,ema_qfq_20,ema_qfq_60,kdj_k_qfq,kdj_d_qfq"
    )

    for i, code in enumerate(sorted(codes)):
        if not code.strip():
            continue

        # stk_factor_pro — 只拉当日
        key = ("stk_factor_pro", code, trade_date)
        existing = s.read_per_stock("stk_factor_pro", code, trade_date)
        if existing is None or existing.empty:
            time.sleep(interval)
            try:
                df = pro.stk_factor_pro(ts_code=code, trade_date=trade_date, fields=stk_factor_fields)
                if df is not None and not df.empty:
                    s.write_per_stock("stk_factor_pro", code, trade_date, df)
            except Exception:
                pass

        # daily_basic — 只拉当日
        existing = s.read_per_stock("daily_basic", code, trade_date)
        if existing is None or existing.empty:
            basic_fields = "ts_code,trade_date,turnover_rate,turnover_rate_f,circ_mv,total_mv,pe,pe_ttm,pb,ps,ps_ttm"
            time.sleep(interval * 0.5)
            try:
                df = pro.daily_basic(ts_code=code, trade_date=trade_date, fields=basic_fields)
                if df is not None and not df.empty:
                    s.write_per_stock("daily_basic", code, trade_date, df)
            except Exception:
                pass

        # daily 历史 — 按整段拉取（pro.daily 会自动缓存到 per_stock）
        existing_all = s.read_per_stock_all("daily", code)
        if existing_all is None or existing_all.empty:
            time.sleep(interval * 0.5)
            try:
                df = pro.daily(ts_code=code, start_date=start_date, end_date=trade_date)
                if df is not None and not df.empty:
                    # 按日期分组一次性写入，避免逐行 iterrows
                    df["_td"] = df["trade_date"].astype(str).str.replace("-", "").str[:8]
                    for td, chunk in df.groupby("_td"):
                        if len(td) == 8 and td.isdigit():
                            s.write_per_stock("daily", code, td, chunk.drop(columns=["_td"]))
            except Exception:
                pass

        if (i + 1) % 50 == 0:
            print(f"   个股预拉取: {i + 1}/{len(codes)}")

    print(f"   个股预拉取完成")


def _fetch_daily_candidates(pro, trade_date: str) -> set[str]:
    """日常快速候选池：涨停 + 同花顺热榜前20板块成分股（≈2000只）。
    
    用于 run_fetch 日常增量，比 _fetch_concept_candidates（全量板块4128只）快得多。
    """
    candidate_codes: set[str] = set()

    # 来源 1: 涨停股
    try:
        limit_df = pro.limit_list_d(trade_date=trade_date)
        if limit_df is not None and not limit_df.empty:
            codes = limit_df["ts_code"].astype(str).tolist()
            candidate_codes.update(codes)
            print(f"  [CAND] limit_list_d: {len(codes)} 只涨停")
    except Exception:
        pass

    # 来源 2: 同花顺热榜前20板块成分股
    try:
        hot = pro.ths_hot(trade_date=trade_date)
        if hot is not None and not hot.empty:
            hot = hot[hot["data_type"].isin(["概念板块", "行业板块"])]
            hot = hot.sort_values("rank").head(20)
            for _, row in hot.iterrows():
                idx = row["ts_code"]
                if not isinstance(idx, str) or not idx.endswith(".TI"):
                    continue
                time.sleep(_INTERVAL * 0.5)
                try:
                    memb = pro.ths_member(ts_code=idx)
                    if memb is not None and not memb.empty:
                        codes = memb["con_code"].astype(str).tolist()
                        candidate_codes.update(codes)
                except Exception:
                    continue
            print(f"  [CAND] 热榜前20板块成分: 去重后 {len(candidate_codes)} 只")
    except Exception:
        pass

    # 去重北交所
    candidate_codes = {c for c in candidate_codes if not c.endswith(".BJ")}
    print(f"  [CAND] 日常候选池合计: {len(candidate_codes)} 只")

    if not candidate_codes:
        print("  [CAND] 无候选股，跳过")
        return set()
    return candidate_codes


def _fetch_concept_candidates(pro, trade_date: str) -> set[str]:
    """从 concepts.yaml 全量板块/概念获取成分股 + 涨停股，覆盖所有策略需要的候选池。"""
    concepts = load_concepts()
    all_themes = concepts.get_themes()
    theme_names = list(all_themes) if all_themes else []
    alias_map = concepts.get_aliases()

    # 用概念关键词反查 THS 板块
    keyword_list = set(theme_names)
    for aliases in alias_map.values():
        for a in aliases:
            keyword_list.add(a)
    keyword_list = sorted(k for k in keyword_list if len(k) >= 2)

    candidate_codes: set[str] = set()

    # 来源 1: 涨停股
    try:
        limit_df = pro.limit_list_d(trade_date=trade_date)
        if limit_df is not None and not limit_df.empty:
            codes = limit_df["ts_code"].astype(str).tolist()
            candidate_codes.update(codes)
            print(f"  [CAND] limit_list_d: {len(codes)} 只涨停")
    except Exception:
        pass

    # 来源 2: 从 concepts.yaml 中所有板块/概念获取 THS 指数 → 成分股
    try:
        idx = s.read_static("ths_index")
        if idx is None or idx.empty:
            idx = pro.ths_index()
            if idx is not None and not idx.empty:
                s.write_static("ths_index", idx)

        # 用 keyword_list 匹配 THS 板块（改用 regex=False 避免正则警告）
        matched = set()
        for kw in keyword_list:
            hits = idx[idx["name"].str.contains(kw, na=False, regex=False)]
            matched.update(hits["ts_code"].tolist())
        matched = {c for c in matched if isinstance(c, str) and c.endswith(".TI")}
        print(f"  [CAND] 匹配到 {len(matched)} 个 THS 板块（来自 {len(keyword_list)} 个关键词）")

        board_count = 0
        for ti_code in sorted(matched):
            time.sleep(_INTERVAL)  # 拉成分股也需限流
            try:
                memb = pro.ths_member(ts_code=ti_code)
                if memb is not None and not memb.empty:
                    codes = memb["con_code"].astype(str).tolist()
                    candidate_codes.update(codes)
                    board_count += 1
            except Exception:
                continue
        print(f"  [CAND] concepts.yaml 板块: {board_count} 个板块/概念, 成分股去重后 {len(candidate_codes)} 只")
    except Exception:
        pass

    # 去重北交所
    candidate_codes = {c for c in candidate_codes if not c.endswith(".BJ")}

    if not candidate_codes:
        print("  [CAND] 无候选股，跳过")
        return set()

    return candidate_codes


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


def fill_per_stock_cache(
    trade_date: Optional[str] = None,
    lookback: int = 60,
    token: str = "",
):
    """仅拉取 per-stock 数据（stk_factor_pro + daily 历史 + daily_basic），后台补全用。
    
    用于首次全量填充：拉完所有概念板块成分股的个股历史数据。
    后续增量时 run_fetch 内的 _prefetch_per_stock_data 会自动跳过已有缓存。
    """
    pro = _get_pro(token)
    today = datetime.now().strftime("%Y%m%d")
    if not trade_date:
        trade_date = _latest_open_date(pro, today)
    print(f"交易日: {trade_date}")
    print(f"回看天数: {lookback}")

    candidate_codes = _fetch_concept_candidates(pro, trade_date)
    if candidate_codes:
        print(f"\n🎯 后台填充: 为 {len(candidate_codes)} 只候选股补全 per-stock 数据...")
        print(f"   间隔 {_FILL_INTERVAL}s（≤75次/分钟），预计耗时约 {len(candidate_codes) * _FILL_INTERVAL / 60:.0f} 分钟")
        _prefetch_per_stock_data(pro, candidate_codes, trade_date, lookback, interval=_FILL_INTERVAL)
    print("✅ per-stock 缓存填充完成")


def main():
    p = argparse.ArgumentParser(description="统一预拉取 — 将 Tushare 数据写入 Parquet 仓库")
    p.add_argument("--date", help="交易日 YYYYMMDD（默认最近交易日）")
    p.add_argument("--lookback", type=int, default=60, help="回看天数（默认 60）")
    p.add_argument("--token", default="", help="Tushare Token")
    p.add_argument("--clear-cache", action="store_true", help="清空 Parquet 缓存后退出")
    p.add_argument("--fill-per-stock", action="store_true",
                    help="仅执行 per-stock 数据填充（后台补全用），不会重复拉已有缓存")
    args = p.parse_args()

    if args.clear_cache:
        s.clear_cache()
        print("Parquet 缓存已清空")
        return

    if args.fill_per_stock:
        fill_per_stock_cache(trade_date=args.date, lookback=args.lookback, token=args.token)
        return

    run_fetch(trade_date=args.date, lookback=args.lookback, token=args.token)


if __name__ == "__main__":
    main()
