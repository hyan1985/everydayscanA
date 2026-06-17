#!/usr/bin/env python3
"""
更新短线跟踪登记表：
- 首次入选当天价格作为基准价
- 记录跟踪天数
- 更新当日价格
- 给出单票胜负判断与总体胜率
"""

import argparse
import os
from pathlib import Path

import pandas as pd


REQUIRED_COLS = ["ts_code", "name", "latest_trade_date", "latest_close", "total_score", "concept_name"]


def to_date(x):
    return pd.to_datetime(x, errors="coerce").date() if pd.notna(x) else None


def fetch_latest_close_map(token: str, codes: list[str], trade_date: pd.Timestamp) -> dict[str, float]:
    """
    拉取指定交易日的收盘价映射（ts_code -> close）。
    若无token或接口异常，返回空映射。
    """
    if not token or not codes:
        return {}
    try:
        import tushare as ts
    except Exception:
        return {}

    pro = ts.pro_api(token)
    td = trade_date.strftime("%Y%m%d")
    out: dict[str, float] = {}
    for code in codes:
        try:
            df = pro.daily(ts_code=code, start_date=td, end_date=td, fields="ts_code,trade_date,close")
        except Exception:
            continue
        if df is None or df.empty:
            continue
        c = pd.to_numeric(df.iloc[0].get("close"), errors="coerce")
        if pd.notna(c):
            out[str(code)] = float(c)
    return out


def main():
    p = argparse.ArgumentParser(description="更新股票跟踪登记表")
    p.add_argument("--input", required=True, help="每日选股CSV路径")
    p.add_argument("--register", default="output/journal/tracking_register.csv", help="登记表CSV路径")
    args = p.parse_args()

    input_path = Path(args.input)
    reg_path = Path(args.register)
    if not input_path.exists():
        raise FileNotFoundError(f"找不到输入文件: {input_path}")

    daily = pd.read_csv(input_path)
    missing = [c for c in REQUIRED_COLS if c not in daily.columns]
    if missing:
        print(f"⚠ 输入文件缺少字段({missing})，跳过跟踪表更新（CSV 结构已变更）。")
        return

    daily = daily.copy()
    daily["latest_trade_date"] = pd.to_datetime(daily["latest_trade_date"], errors="coerce")
    daily["total_score"] = pd.to_numeric(daily["total_score"], errors="coerce")
    daily["concept_name"] = daily["concept_name"].fillna("未知板块")
    daily = daily.dropna(subset=["ts_code", "latest_trade_date", "latest_close"])
    if daily.empty:
        print("今日选股为空，登记表未更新。")
        return
    current_trade_date = daily["latest_trade_date"].max()

    # 计算板块内评分排名（1为最高分）
    daily["concept_stock_count"] = daily.groupby("concept_name")["ts_code"].transform("count")
    daily["concept_score_rank"] = (
        daily.groupby("concept_name")["total_score"].rank(ascending=False, method="min").astype("Int64")
    )

    if reg_path.exists():
        reg = pd.read_csv(reg_path)
    else:
        reg = pd.DataFrame(
            columns=[
                "ts_code",
                "name",
                "entry_type",
                "is_trade_signal",
                "selection_tier",
                "first_in_date",
                "base_price",
                "last_seen_date",
                "last_price",
                "total_score",
                "concept_score_rank",
                "concept_stock_count",
                "tracking_days",
                "return_pct",
                "is_win",
                "win_judgement",
                "rule_remark",
                "appear_count",
                "industry",
                "concept_name",
            ]
        )

    reg = reg.copy()
    if not reg.empty:
        reg["first_in_date"] = pd.to_datetime(reg["first_in_date"], errors="coerce")
        reg["last_seen_date"] = pd.to_datetime(reg["last_seen_date"], errors="coerce")

    indexed = reg.set_index("ts_code", drop=False) if not reg.empty else reg
    today_codes = set(str(x).strip() for x in daily["ts_code"].tolist())

    for _, row in daily.iterrows():
        code = str(row["ts_code"]).strip()
        trade_date = pd.to_datetime(row["latest_trade_date"])
        px = float(row["latest_close"])
        total_score = float(row["total_score"]) if pd.notna(row.get("total_score")) else None
        name = str(row.get("name", ""))
        entry_type = str(row.get("entry_type", "none"))
        is_trade_signal = bool(row.get("is_trade_signal", entry_type != "none"))
        selection_tier = str(row.get("selection_tier", "signal" if is_trade_signal else "watchlist"))
        industry = str(row.get("industry", ""))
        concept = str(row.get("concept_name", ""))
        concept_rank = int(row["concept_score_rank"]) if pd.notna(row.get("concept_score_rank")) else None
        concept_cnt = int(row["concept_stock_count"]) if pd.notna(row.get("concept_stock_count")) else None

        if code in indexed.index:
            indexed.at[code, "name"] = name
            indexed.at[code, "entry_type"] = entry_type
            indexed.at[code, "is_trade_signal"] = is_trade_signal
            indexed.at[code, "selection_tier"] = selection_tier
            indexed.at[code, "last_seen_date"] = trade_date
            indexed.at[code, "last_price"] = px
            indexed.at[code, "total_score"] = total_score
            indexed.at[code, "concept_score_rank"] = concept_rank
            indexed.at[code, "concept_stock_count"] = concept_cnt
            indexed.at[code, "industry"] = industry
            indexed.at[code, "concept_name"] = concept
            prev_count = indexed.at[code, "appear_count"]
            prev_count = int(prev_count) if pd.notna(prev_count) else 0
            indexed.at[code, "appear_count"] = prev_count + 1
        else:
            new_row = {
                "ts_code": code,
                "name": name,
                "entry_type": entry_type,
                "is_trade_signal": is_trade_signal,
                "selection_tier": selection_tier,
                "first_in_date": trade_date,
                "base_price": px,
                "last_seen_date": trade_date,
                "last_price": px,
                "total_score": total_score,
                "concept_score_rank": concept_rank,
                "concept_stock_count": concept_cnt,
                "tracking_days": 1,
                "return_pct": 0.0,
                "is_win": 0,
                "win_judgement": "平",
                "rule_remark": "继续持有观察",
                "appear_count": 1,
                "industry": industry,
                "concept_name": concept,
            }
            if indexed.empty:
                indexed = pd.DataFrame([new_row]).set_index("ts_code", drop=False)
            else:
                indexed = pd.concat([indexed, pd.DataFrame([new_row]).set_index("ts_code", drop=False)])

    # 对“今日未入选但仍在登记表”的股票，也推进日期并尝试刷新当日价格
    if not indexed.empty and pd.notna(current_trade_date):
        stale_codes = [c for c in indexed.index.tolist() if str(c) not in today_codes]
        token = os.getenv("TUSHARE_TOKEN", "").strip()
        close_map = fetch_latest_close_map(token=token, codes=stale_codes, trade_date=current_trade_date)
        for code in stale_codes:
            indexed.at[code, "last_seen_date"] = current_trade_date
            if code in close_map:
                indexed.at[code, "last_price"] = close_map[code]

    # 统一计算派生字段
    indexed["tracking_days"] = (
        (pd.to_datetime(indexed["last_seen_date"]) - pd.to_datetime(indexed["first_in_date"])).dt.days + 1
    )
    indexed["return_pct"] = (
        (pd.to_numeric(indexed["last_price"], errors="coerce") / pd.to_numeric(indexed["base_price"], errors="coerce") - 1) * 100
    )
    indexed["return_pct"] = indexed["return_pct"].fillna(0.0)
    indexed["is_win"] = (indexed["return_pct"] > 0).astype(int)
    indexed["win_judgement"] = indexed["return_pct"].apply(lambda x: "赢" if x > 0 else ("平" if abs(x) < 1e-12 else "亏"))
    # 规则备注：按你的执行纪律自动标注
    indexed["rule_remark"] = indexed["return_pct"].apply(
        lambda x: "触发止盈离场（+5%）" if x >= 5.0 else ("触发止损离场（-4.5%）" if x <= -4.5 else "继续持有观察")
    )

    out = indexed.reset_index(drop=True).sort_values(["return_pct", "tracking_days"], ascending=[False, False])
    out["first_in_date"] = pd.to_datetime(out["first_in_date"]).dt.strftime("%Y-%m-%d")
    out["last_seen_date"] = pd.to_datetime(out["last_seen_date"]).dt.strftime("%Y-%m-%d")
    out["base_price"] = pd.to_numeric(out["base_price"], errors="coerce").round(2)
    out["last_price"] = pd.to_numeric(out["last_price"], errors="coerce").round(2)
    out["total_score"] = pd.to_numeric(out["total_score"], errors="coerce").round(2)
    out["return_pct"] = pd.to_numeric(out["return_pct"], errors="coerce").round(2)

    out["is_trade_signal"] = out["is_trade_signal"].astype(bool)

    reg_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(reg_path, index=False, encoding="utf-8-sig")

    win_rate = float(out["is_win"].mean()) if len(out) > 0 else 0.0
    signal_mask = out["is_trade_signal"] == True
    signal_win_rate = float(out.loc[signal_mask, "is_win"].mean()) if signal_mask.any() else 0.0
    print(f"登记表已更新: {reg_path}")
    print(f"当前跟踪股票数: {len(out)} | 全样本胜率: {win_rate:.2%} | 交易信号样本胜率: {signal_win_rate:.2%}")


if __name__ == "__main__":
    main()
