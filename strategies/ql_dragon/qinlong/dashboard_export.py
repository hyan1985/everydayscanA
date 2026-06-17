"""扫描结束后更新 Cursor Canvas 仪表盘（改写 canvases 内标记块）。"""

from __future__ import annotations

import base64
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from qinlong.strategy_policy import strategy_bucket

MARK_START = "// <qinlong:auto-dashboard:start>"
MARK_END = "// <qinlong:auto-dashboard:end>"

DEFAULT_CANVAS = Path.home() / ".cursor/projects/Users-hyan-Desktop/canvases/qinlong-hunter-dashboard.canvas.tsx"


def _fmt_num(x: Any, nd: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    try:
        return str(round(float(x), nd))
    except (TypeError, ValueError):
        return str(x)


def _build_structure(row: pd.Series) -> str:
    parts: list[str] = []
    if bool(row.get("from_limit_list")):
        parts.append("当日涨停")
    tags = str(row.get("concept_tags") or "").strip()
    if tags:
        parts.append("热榜题材映射")
    ratio = row.get("close_to_prev_high")
    if ratio is not None and not pd.isna(ratio):
        r = float(ratio)
        if r >= 1.03:
            parts.append("收于阶段前高参考带之上（突破口径）")
        elif r >= 1.0:
            parts.append("贴近/触及阶段前高")
        elif r >= 0.95:
            parts.append("距阶段前高不远，处于蓄势区")
        else:
            parts.append("仍在压力位下方整理")
    volr = row.get("vol_to_ma5")
    if volr is not None and not pd.isna(volr):
        v = float(volr)
        if v >= 1.5:
            parts.append("量能放大")
        elif v >= 1.0:
            parts.append("量能温和")
        else:
            parts.append("量能偏弱")
    return "；".join(parts) if parts else "综合打分入围"


def _build_explain(row: pd.Series) -> str:
    theme = float(row.get("s_theme") or 0)
    tech = float(row.get("s_technical") or 0)
    chip = float(row.get("s_chip") or 0)
    news = float(row.get("s_news") or 0)
    fina = float(row.get("s_fundamental") or 0)
    ratio = row.get("close_to_prev_high")
    rtxt = ""
    if ratio is not None and not pd.isna(ratio):
        rtxt = f"收盘相对阶段前高参考比约 {_fmt_num(ratio, 4)}。"
    parts = []
    if theme >= 8:
        parts.append("题材分项强：更像主叙事品种，短期更易吸引合力。")
    elif theme >= 5:
        parts.append("题材分项中等：需确认催化是否可持续。")
    else:
        parts.append("题材分项一般：偏跟风或映射薄弱。")
    if tech >= 7:
        parts.append(f"技术面分项偏强：均线/MACD 共振较好；{rtxt}")
    elif tech >= 4:
        parts.append(f"技术面中性：突破或中继信号不完全一致；{rtxt}")
    else:
        parts.append(f"技术面偏弱：动能与形态尚未形成共振；{rtxt}")
    if chip >= 8:
        parts.append("筹码分项偏高：获利盘较重，波动与分歧可能放大。")
    elif chip >= 5:
        parts.append("筹码分项中性：更像常规换手区。")
    else:
        parts.append("筹码分项偏低：位置或情绪不一定占优。")
    if news >= 6:
        parts.append("资金热度代理分项偏高（龙虎榜/大单口径叠加）。")
    if fina >= 7:
        parts.append("基本面分项不差：业绩叙事更易形成闭环。")
    return "".join(parts)


def _build_action(row: pd.Series) -> str:
    tech = float(row.get("s_technical") or 0)
    theme = float(row.get("s_theme") or 0)
    ratio = row.get("close_to_prev_high")
    parts = ["纪律优先：不满足共振则观望。"]
    if ratio is not None and not pd.isna(ratio) and float(ratio) >= 1.03 and tech >= 5:
        parts.append("突破确认后不宜追高，优先等回踩前高转支撑或缩量整理后再放量。")
    elif theme >= 8 and tech < 5:
        parts.append("题材先行、走势滞后：防止情绪末端接力，等待结构与量能共振。")
    else:
        parts.append("若参与仅试错仓；假突破参考：跌回前高参考带下约3%且三日收不回需降风险。")
    parts.append("加仓仅在看懂趋势延续且量能健康后金字塔推进。")
    return "".join(parts)


def dataframe_to_payload(df: pd.DataFrame, trade_date: str) -> dict[str, Any]:
    rows_out: list[dict[str, str]] = []
    for _, row in df.iterrows():
        strat_label, _, strat_note = strategy_bucket(row)
        rows_out.append(
            {
                "score": _fmt_num(row.get("score"), 2),
                "tsCode": str(row.get("ts_code") or ""),
                "name": str(row.get("name") or ""),
                "industry": str(row.get("industry") or ""),
                "tradeDate": trade_date,
                "theme": _fmt_num(row.get("s_theme"), 2),
                "news": _fmt_num(row.get("s_news"), 2),
                "technical": _fmt_num(row.get("s_technical"), 2),
                "fundamental": _fmt_num(row.get("s_fundamental"), 2),
                "chip": _fmt_num(row.get("s_chip"), 2),
                "strategy": strat_label,
                "strategyNote": strat_note,
                "structure": _build_structure(row),
                "explain": _build_explain(row),
                "action": _build_action(row),
            }
        )
    return {
        "tradeDate": trade_date,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": rows_out,
    }


def payload_to_b64_line(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return f'const AUTO_SNAPSHOT_B64 = "{b64}";'


def patch_canvas(canvas_path: Path, payload: dict[str, Any]) -> None:
    text = canvas_path.read_text(encoding="utf-8")
    if MARK_START not in text or MARK_END not in text:
        raise ValueError(f"Canvas 文件缺少标记块：{MARK_START} / {MARK_END}")
    new_line = payload_to_b64_line(payload)
    pattern = re.compile(
        re.escape(MARK_START) + r"\s*.*?\s*" + re.escape(MARK_END),
        re.DOTALL,
    )
    block = f"{MARK_START}\n{new_line}\n{MARK_END}"
    updated, n = pattern.subn(block, text, count=1)
    if n != 1:
        raise RuntimeError("未能替换 Canvas 自动区块（标记重复或缺失）。")
    canvas_path.write_text(updated, encoding="utf-8")


def export_from_csv(csv_path: Path, trade_date: str, *, canvas_path: Path | None = None) -> Path:
    df = pd.read_csv(csv_path)
    if df.empty:
        payload = {
            "tradeDate": trade_date,
            "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": [],
        }
    else:
        payload = dataframe_to_payload(df, trade_date)
    out_canvas = canvas_path or Path(os.environ.get("QINLONG_CANVAS_TSX", str(DEFAULT_CANVAS)))
    patch_canvas(out_canvas, payload)
    return out_canvas


def export_from_scan_csv(csv_path: Path, debug_trade_date: str | None) -> Path | None:
    """供 CLI 调用：读扫描 CSV，trade_date 优先用参数，否则读首行或文件名。"""
    if not csv_path.is_file():
        return None
    df = pd.read_csv(csv_path)
    td = debug_trade_date
    if not td and not df.empty and "trade_date" in df.columns:
        td = str(df.iloc[0]["trade_date"])
    if not td:
        td = datetime.now().strftime("%Y%m%d")
    return export_from_csv(csv_path, td)
