"""候选池：涨停 + 龙虎榜 + 同花顺热榜板块成分。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from qinlong.scanner.throttle import TushareThrottle


def _norm_hot_types(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    keep = df["data_type"].isin(["概念板块", "行业板块"])
    return df.loc[keep].copy()


@dataclass
class _Acc:
    best_concept_rank: float | None = None
    tags: set[str] = field(default_factory=set)
    from_limit_list: bool = False
    from_hm_detail: bool = False


def collect_candidates(
    pro,
    trade_date: str,
    throttle: TushareThrottle,
    *,
    top_concepts: int = 12,
    max_members_per_concept: int = 120,
    max_total_from_concepts: int = 220,
    skip_hm_detail: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    合并三类来源，输出候选股票及标签。

    Returns:
        meta 每行对应 ts_code：best_concept_rank, concept_tags, 涨停/龙虎榜标记等。
        debug 调试信息（计数等）。
    """
    debug: dict[str, Any] = {}

    throttle.pace_before("general")
    try:
        limit_df = pro.limit_list_d(trade_date=trade_date)
    except Exception as exc:
        limit_df = None
        debug["limit_list_d_error"] = str(exc)
    finally:
        throttle.mark_after("general")

    debug["limit_list_d"] = 0 if limit_df is None else len(limit_df)

    hm_df = None
    if not skip_hm_detail:
        throttle.pace_before("hm_detail")
        try:
            hm_df = pro.hm_detail(trade_date=trade_date)
        except Exception as exc:
            hm_df = None
            debug["hm_detail_error"] = str(exc)
        finally:
            throttle.mark_after("hm_detail")
    else:
        debug["hm_detail_skipped"] = True

    debug["hm_detail"] = 0 if hm_df is None else len(hm_df)

    throttle.pace_before("general")
    try:
        hot_raw = pro.ths_hot(trade_date=trade_date)
    except Exception as exc:
        hot_raw = None
        debug["ths_hot_error"] = str(exc)
    finally:
        throttle.mark_after("general")

    hot_df = _norm_hot_types(hot_raw) if hot_raw is not None else pd.DataFrame()
    if hot_df is None or hot_df.empty or "rank" not in hot_df.columns:
        hot_df = pd.DataFrame()
        debug["ths_hot_used"] = 0
    else:
        hot_df = hot_df.sort_values(["rank", "data_type"]).head(top_concepts * 3)
        hot_df = hot_df.drop_duplicates(subset=["ts_code"]).head(top_concepts)
        debug["ths_hot_used"] = len(hot_df)

    stock_best_rank: dict[str, float] = {}
    stock_concepts: dict[str, set[str]] = defaultdict(set)

    member_rows = 0
    for _, row in hot_df.iterrows():
        idx_code = row["ts_code"]
        if not isinstance(idx_code, str) or not idx_code.endswith(".TI"):
            continue
        rank = float(row.get("rank", 99))
        name = str(row.get("ts_name") or "")
        throttle.pace_before("general")
        try:
            members = pro.ths_member(ts_code=idx_code)
        except Exception:
            members = None
        finally:
            throttle.mark_after("general")
        if members is None or members.empty:
            continue
        take = members.head(max_members_per_concept)
        member_rows += len(take)
        for con in take["con_code"].astype(str).tolist():
            prev = stock_best_rank.get(con)
            if prev is None or rank < prev:
                stock_best_rank[con] = rank
            tag = f"{name}({idx_code})"
            stock_concepts[con].add(tag)

        if len(stock_best_rank) >= max_total_from_concepts:
            break

    debug["concept_member_rows"] = member_rows
    debug["unique_from_concepts"] = len(stock_best_rank)

    hm_net_by_code: dict[str, float] = defaultdict(float)
    if hm_df is not None and not hm_df.empty:
        for _, r in hm_df.iterrows():
            ts = str(r["ts_code"])
            hm_net_by_code[ts] += float(r.get("net_amount") or 0.0)

    acc: dict[str, _Acc] = {}

    def _get(ts: str) -> _Acc:
        if ts not in acc:
            acc[ts] = _Acc()
        return acc[ts]

    for ts, net in hm_net_by_code.items():
        a = _get(ts)
        a.from_hm_detail = True

    if limit_df is not None and not limit_df.empty:
        for _, r in limit_df.iterrows():
            ts = str(r["ts_code"])
            a = _get(ts)
            a.from_limit_list = True
            if ts in stock_concepts:
                a.tags |= stock_concepts[ts]
            rnk = stock_best_rank.get(ts)
            if rnk is not None:
                a.best_concept_rank = (
                    rnk if a.best_concept_rank is None else min(a.best_concept_rank, rnk)
                )

    for ts, rnk in stock_best_rank.items():
        a = _get(ts)
        a.best_concept_rank = rnk if a.best_concept_rank is None else min(a.best_concept_rank, rnk)
        a.tags |= stock_concepts[ts]

    rows: list[dict[str, Any]] = []
    for ts, a in acc.items():
        rows.append(
            {
                "ts_code": ts,
                "best_concept_rank": a.best_concept_rank,
                "concept_tags": " | ".join(sorted(a.tags))[:800],
                "from_limit_list": a.from_limit_list,
                "from_hm_detail": a.from_hm_detail,
                "hm_net_amount": hm_net_by_code.get(ts),
            }
        )

    if not rows:
        meta = pd.DataFrame(
            columns=[
                "ts_code",
                "best_concept_rank",
                "concept_tags",
                "from_limit_list",
                "from_hm_detail",
                "hm_net_amount",
            ]
        )
        return meta, debug

    meta = pd.DataFrame(rows)
    meta = meta.sort_values(
        by=["best_concept_rank", "from_limit_list", "from_hm_detail"],
        ascending=[True, False, False],
        na_position="last",
    )
    return meta, debug
