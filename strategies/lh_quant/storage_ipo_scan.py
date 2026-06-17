#!/usr/bin/env python3
"""长鑫 / 长江存储 IPO 供应链 — 简易「可上车」扫描。"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config" / "storage_ipo.yaml"
OUT_DIR = ROOT / "output" / "daily"


def _load_token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    p = ROOT / ".secrets" / "tushare_token"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    raise SystemExit("未找到 TUSHARE_TOKEN")


def _latest_open_date(pro, end: str) -> str:
    cal = pro.trade_cal(exchange="SSE", end_date=end, is_open="1")
    if cal is None or cal.empty:
        return end.replace("-", "")
    return str(cal["cal_date"].max())


def _build_pool(pro, cfg: dict) -> pd.DataFrame:
    kws = cfg.get("search_keywords", [])
    idx = pro.ths_index()
    matched = {
        str(r["ts_code"]): str(r["name"])
        for _, r in idx.iterrows()
        if any(kw in str(r["name"]) for kw in kws)
    }

    frames = []
    for code, name in matched.items():
        time.sleep(0.12)
        m = pro.ths_member(ts_code=code)
        if m is None or m.empty:
            continue
        frames.append(
            pd.DataFrame(
                {
                    "ts_code": m["con_code"].astype(str),
                    "ths_concept": name,
                }
            )
        )
    if not frames:
        return pd.DataFrame()

    pool = pd.concat(frames, ignore_index=True)
    priority = re.compile(r"长鑫|长江|长存|存储|DRAM|NAND|闪存|HBM|封测|设备|光刻|硅片")

    def pick_concept(grp: pd.DataFrame) -> str:
        names = grp["ths_concept"].tolist()
        for n in names:
            if priority.search(n):
                return n
        return names[0]

    concept_map = pool.groupby("ts_code").apply(pick_concept)
    pool = pool.drop_duplicates("ts_code").copy()
    pool["主概念"] = pool["ts_code"].map(concept_map)

    basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
    pool = pool.merge(basic.rename(columns={"name": "股票名称"}), on="ts_code", how="left")

    exclude = set(cfg.get("exclude_codes", []))
    pool = pool[~pool["ts_code"].isin(exclude)]
    pool = pool[~pool["股票名称"].astype(str).str.contains("ST", na=False)]

    def tier(row: pd.Series) -> str:
        name = str(row["股票名称"])
        industry = str(row.get("industry", ""))
        concept = str(row["主概念"])
        n = f"{name}{industry}{concept}"

        if re.search(r"长鑫|长江存储|长存", n):
            return "主题直接"
        # 名称优先，避免「存储芯片」概念把设备股误标为模组
        if re.search(
            r"北方华创|中微公司|拓荆|盛美|华海清科|精测|芯源|万业|至纯|长川|华峰|"
            r"微导|屹唐|京仪|富创|中科飞测|新莱应材|东材|中瓷电子|中船特气",
            name,
        ):
            return "半导体设备"
        if re.search(
            r"雅克|安集|鼎龙|江丰|华特|彤程|沪硅|中环|特气|靶材|硅片|湿电子|南大光电|"
            r"晶瑞|广钢|金宏|华海|格林达|多氟多|新宙邦",
            name,
        ):
            return "材料与零部件"
        if re.search(r"长电|通富|华天|甬矽|晶方|颀中|伟测|利扬|气派|封测|封装", name):
            return "制造封测"
        if re.search(
            r"兆易|佰维|江波龙|澜起|德明利|北京君正|普冉|东芯|聚辰|国科微|"
            r"深科技|香农|朗科|同有",
            name,
        ):
            return "存储设计/模组"

        if re.search(r"封测|封装|Chiplet", n):
            return "制造封测"
        if re.search(r"设备|刻蚀|薄膜|光刻|CMP|检测|真空", n):
            return "半导体设备"
        if re.search(r"特气|靶材|硅片|湿电子|材料|气体|化学品|胶", n):
            return "材料与零部件"
        if re.search(r"DRAM|NAND|闪存|存储芯片|内存|HBM", n):
            return "存储设计/模组"
        return "产业链关联"

    pool["供应链环节"] = pool.apply(tier, axis=1)
    return pool


def _attach_quotes(pro, pool: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    db = pro.daily_basic(
        trade_date=trade_date,
        fields="ts_code,close,turnover_rate,circ_mv",
    )
    daily = pro.daily(
        trade_date=trade_date,
        fields="ts_code,pct_chg,amount",
    )
    mf = pro.moneyflow(trade_date=trade_date, fields="ts_code,net_mf_amount")
    out = pool.merge(db, on="ts_code", how="left").merge(daily, on="ts_code", how="left")
    if mf is not None and not mf.empty:
        out = out.merge(mf, on="ts_code", how="left")
    else:
        out["net_mf_amount"] = float("nan")
    out["流通市值(亿)"] = out["circ_mv"] / 10000
    out["成交额(亿)"] = out["amount"] / 100000
    out["主力净流入(万)"] = out["net_mf_amount"]  # net_mf_amount 已是万元，无需再除
    return out


def _score_action(row: pd.Series, cfg: dict) -> tuple[str, str, float]:
    """返回 (档位, 说明, 排序分)。"""
    entry = cfg["entry"]
    watch = cfg["watch"]
    tier = str(row.get("供应链环节", ""))
    pct = float(row["pct_chg"]) if pd.notna(row.get("pct_chg")) else 0.0
    to = float(row["turnover_rate"]) if pd.notna(row.get("turnover_rate")) else 0.0
    mv = float(row["流通市值(亿)"]) if pd.notna(row.get("流通市值(亿)")) else 0.0
    amt = float(row["成交额(亿)"]) if pd.notna(row.get("成交额(亿)")) else 0.0
    mf = row.get("主力净流入(万)")

    core_tiers = set(cfg.get("core_tiers", []))
    watch_tiers = set(cfg.get("watch_tiers", []))

    if tier not in core_tiers and tier not in watch_tiers:
        return "跳过", "非核心供应链环节", -1.0

    if pct > watch.get("pct_chg_max", 7) or pct < entry.get("pct_chg_min", -3):
        return "放弃", f"涨跌幅{pct:.1f}%超范围", -1.0
    if to > watch.get("turnover_max", 25):
        return "放弃", "换手过高", -1.0

    score = 50.0
    reasons = []

    if tier in core_tiers:
        score += 15
        reasons.append("核心环节")
    if entry["pct_chg_min"] <= pct <= entry["pct_chg_max"]:
        score += 20
        reasons.append("涨幅适中可低吸")
    elif pct <= watch.get("pct_chg_max", 7):
        score += 5
        reasons.append("偏强仅观察")
    else:
        return "放弃", "涨幅不适配", -1.0

    if entry["turnover_min"] <= to <= entry["turnover_max"]:
        score += 10
    if mv >= entry.get("circ_mv_min_yi", 80):
        score += 5
    if amt >= entry.get("amount_min_yi", 1.5):
        score += 5
    if pd.notna(mf) and float(mf) > 0:
        score += 8
        reasons.append("主力净流入")
    elif entry.get("prefer_moneyflow_positive") and pd.notna(mf) and float(mf) < 0:
        score -= 5
        reasons.append("主力流出")

    # 判定档位
    if (
        tier in core_tiers
        and entry["pct_chg_min"] <= pct <= entry["pct_chg_max"]
        and entry["turnover_min"] <= to <= entry["turnover_max"]
        and mv >= entry.get("circ_mv_min_yi", 80)
        and amt >= entry.get("amount_min_yi", 1.5)
    ):
        if entry.get("prefer_moneyflow_positive") and pd.notna(mf) and float(mf) < 0:
            return "观察", "核心环节但主力流出", score - 10
        return "可做", "；".join(reasons) or "条件满足", score

    if tier in watch_tiers or tier in core_tiers:
        return "观察", "；".join(reasons) or "接近条件", score

    return "跳过", "", -1.0


def run_scan(trade_date: str | None = None, refresh_pool: bool = False) -> Path:
    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    from quant_data import get_provider

    pro = get_provider()
    today = datetime.now().strftime("%Y%m%d")
    if trade_date:
        td = trade_date.replace("-", "")
    else:
        td = _latest_open_date(pro, today)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pool_path = OUT_DIR / f"cxmt_ymtc_supply_chain_{td}.csv"

    if refresh_pool or not pool_path.exists() or "供应链环节" not in pd.read_csv(
        pool_path, nrows=1, encoding="utf-8-sig"
    ).columns:
        pool = _build_pool(pro, cfg)
        pool = _attach_quotes(pro, pool, td)
        pool.rename(columns={"ts_code": "股票代码"}).to_csv(
            pool_path, index=False, encoding="utf-8-sig"
        )
    else:
        pool = pd.read_csv(pool_path, encoding="utf-8-sig")
        if "ts_code" not in pool.columns and "股票代码" in pool.columns:
            pool["ts_code"] = pool["股票代码"]

    if "pct_chg" not in pool.columns or refresh_pool:
        pool = _attach_quotes(pro, pool, td)

    actions = []
    for _, row in pool.iterrows():
        level, note, sc = _score_action(row, cfg)
        if level == "跳过":
            continue
        actions.append(
            {
                **row.to_dict(),
                "操作档位": level,
                "说明": note,
                "_score": sc,
            }
        )

    act = pd.DataFrame(actions)
    if "股票代码" not in act.columns and "ts_code" in act.columns:
        act["股票代码"] = act["ts_code"]
    if act.empty:
        out_md = OUT_DIR / f"storage_ipo_action_{td}.md"
        out_md.write_text(
            f"# 存储IPO供应链扫描 {td}\n\n当日无符合条件的可上车/观察标的。\n",
            encoding="utf-8",
        )
        return out_md

    tier_prio = {
        "半导体设备": 0,
        "材料与零部件": 1,
        "制造封测": 2,
        "主题直接": 3,
        "存储设计/模组": 4,
        "产业链关联": 5,
    }
    act["_tier_prio"] = act["供应链环节"].map(tier_prio).fillna(9)
    tier_rank = {"可做": 0, "观察": 1, "放弃": 2}
    act["_tier_rank"] = act["操作档位"].map(tier_rank)
    act = act.sort_values(
        ["_tier_rank", "_tier_prio", "_score"], ascending=[True, True, False]
    )

    top_n = int(cfg.get("top_n", 15))
    export_act = act[act["操作档位"].isin(["可做", "观察"])].copy()

    out_csv = OUT_DIR / f"storage_ipo_action_{td}.csv"
    cols = [
        "股票代码",
        "股票名称",
        "供应链环节",
        "主概念",
        "industry",
        "操作档位",
        "说明",
        "close",
        "pct_chg",
        "turnover_rate",
        "流通市值(亿)",
        "成交额(亿)",
        "主力净流入(万)",
    ]
    cols = [c for c in cols if c in export_act.columns]
    export_act[cols].to_csv(out_csv, index=False, encoding="utf-8-sig")

    lines = [
        f"# 存储IPO供应链 · 可上车扫描（{td[:4]}-{td[4:6]}-{td[6:8]}）",
        "",
        "逻辑：长鑫(DRAM) / 长江(NAND) IPO 预期 → 设备 / 材料 / 封测链映射。",
        "已排除：百傲化学(立案)、ST。",
        "",
        f"- 成分池：{len(pool)} 只 | 可做：**{len(act[act['操作档位']=='可做'])}** | 观察：**{len(act[act['操作档位']=='观察'])}**",
        "",
        "## 可做（优先考虑低吸上车）",
        "",
    ]
    ok = act[act["操作档位"] == "可做"].head(top_n)
    if ok.empty:
        lines.append("（无）\n")
    else:
        lines.append("| 代码 | 名称 | 环节 | 涨跌幅% | 换手% | 流通市值(亿) | 主力净流入(万) | 说明 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---|")
        for _, r in ok.iterrows():
            mf = r.get("主力净流入(万)")
            mf_s = f"{float(mf):.0f}" if pd.notna(mf) else "-"
            lines.append(
                f"| {r['股票代码']} | {r['股票名称']} | {r['供应链环节']} | "
                f"{float(r['pct_chg']):.2f} | {float(r['turnover_rate']):.2f} | "
                f"{float(r['流通市值(亿)']):.0f} | {mf_s} | {r['说明']} |"
            )
        lines.append("")

    lines += ["## 观察（偏强或资金分歧）", ""]
    watch = act[act["操作档位"] == "观察"].head(top_n)
    if watch.empty:
        lines.append("（无）\n")
    else:
        lines.append("| 代码 | 名称 | 环节 | 涨跌幅% | 换手% | 说明 |")
        lines.append("|---|---|---|---:|---:|---|")
        for _, r in watch.iterrows():
            lines.append(
                f"| {r['股票代码']} | {r['股票名称']} | {r['供应链环节']} | "
                f"{float(r['pct_chg']):.2f} | {float(r['turnover_rate']):.2f} | {r['说明']} |"
            )

    lines += [
        "",
        "## 规则摘要",
        "",
        "- **可做**：核心环节(设备/材料/封测)，涨幅 -3%～+4.5%，换手 2.5%～18%，流通市值≥80亿，成交额≥1.5亿",
        "- **观察**：模组设计或涨幅偏强 / 主力流出",
        "- 非投资建议；IPO 映射≠持股长鑫/长江",
        "",
        f"完整 CSV：`storage_ipo_action_{td}.csv`",
    ]

    out_md = OUT_DIR / f"storage_ipo_action_{td}.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ {out_md}")
    print(f"✅ {out_csv}")
    print(f"可做 {len(ok)} 只 | 观察 {len(watch)} 只")
    return out_md


def main():
    p = argparse.ArgumentParser(description="长鑫/长江存储 IPO 供应链简易上车扫描")
    p.add_argument("--date", help="交易日 YYYYMMDD")
    p.add_argument("--refresh-pool", action="store_true", help="强制重建成分池")
    args = p.parse_args()
    run_scan(trade_date=args.date, refresh_pool=args.refresh_pool)


if __name__ == "__main__":
    main()
