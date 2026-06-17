from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from .models import StockScore
from .tushare_client import TushareClient


def _read_trade_date(input_path: Path) -> str:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    trade_date = payload.get("meta", {}).get("trade_date")
    if not trade_date:
        raise RuntimeError("输入文件缺少 meta.trade_date，无法做当日胜率跟踪。")
    return str(trade_date)


def _append_history_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    fieldnames = [
        "run_at",
        "pick_date",
        "eval_date",
        "code",
        "name",
        "total_score",
        "pick_close",
        "eval_close",
        "close_vs_pick_close_pct",
        "is_win",
    ]
    existing_keys: set[tuple[str, str, str]] = set()
    rewrite = False
    if exists:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != fieldnames:
                rewrite = True
            else:
                for r in reader:
                    existing_keys.add(
                        (
                            str(r.get("pick_date", "")),
                            str(r.get("eval_date", "")),
                            str(r.get("code", "")),
                        )
                    )
    if rewrite:
        path.rename(path.with_suffix(".csv.bak"))
        exists = False
        existing_keys = set()

    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for row in rows:
            key = (row["pick_date"], row["eval_date"], row["code"])
            if key in existing_keys:
                continue
            writer.writerow(row)


def _render_latest_md(rows: list[dict], output_md: Path) -> None:
    total = len(rows)
    wins = sum(int(r["is_win"]) for r in rows)
    win_rate = (wins / total) if total else 0.0
    avg_pct = sum(float(r["close_vs_pick_close_pct"]) for r in rows) / total if total else 0.0
    lines = [
        f"# 次日胜率跟踪（{rows[0]['pick_date']} -> {rows[0]['eval_date']}）" if rows else "# 次日胜率跟踪（暂无）",
        "",
        f"- 样本数：**{total}**",
        f"- 胜率（今收>昨收）：**{win_rate:.1%}**",
        f"- 平均收益（今收相对昨收）：**{avg_pct:.2f}%**",
        "",
        "| 代码 | 名称 | 评分 | 昨收 | 今收 | 今收-昨收% | 是否胜出 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['code']} | {r['name']} | {r['total_score']} | {r['pick_close']:.2f} | "
            f"{r['eval_close']:.2f} | {r['close_vs_pick_close_pct']:.2f}% | {r['is_win']} |"
        )
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text("\n".join(lines), encoding="utf-8")


def _append_pick_archive(
    archive_path: Path,
    trade_date: str,
    scores: list[StockScore],
    close_map: dict[str, float],
) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    exists = archive_path.exists()
    fieldnames = ["pick_date", "code", "name", "total_score", "pick_close"]
    existing: set[tuple[str, str]] = set()
    if exists:
        with archive_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                existing.add((str(r.get("pick_date", "")), str(r.get("code", ""))))
    with archive_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for s in scores:
            close_px = close_map.get(s.code)
            if close_px is None:
                continue
            key = (trade_date, s.code)
            if key in existing:
                continue
            writer.writerow(
                {
                    "pick_date": trade_date,
                    "code": s.code,
                    "name": s.name,
                    "total_score": s.total_score,
                    "pick_close": round(close_px, 3),
                }
            )


def track_daily_performance(
    input_path: Path,
    scores: list[StockScore],
    history_csv_path: Path = Path("data/performance_history.csv"),
    latest_md_path: Path = Path("reports/performance_latest.md"),
) -> tuple[float, int]:
    trade_date = _read_trade_date(input_path)
    if not scores:
        return 0.0, 0

    pro = TushareClient.from_secure_config().pro()
    df = pro.daily(trade_date=trade_date, fields="ts_code,close")
    if df is None or df.empty:
        raise RuntimeError("未获取到当日 close，无法统计次日胜率。")
    today_close_map = {str(r["ts_code"]): float(r["close"]) for _, r in df.iterrows()}

    pick_archive_path = Path("data/picks_archive.csv")
    _append_pick_archive(
        archive_path=pick_archive_path,
        trade_date=trade_date,
        scores=scores,
        close_map=today_close_map,
    )
    if not pick_archive_path.exists():
        return 0.0, 0

    with pick_archive_path.open("r", encoding="utf-8", newline="") as f:
        pick_rows = list(csv.DictReader(f))
    prev_dates = sorted({str(r["pick_date"]) for r in pick_rows if str(r["pick_date"]) < trade_date})
    if not prev_dates:
        _render_latest_md([], latest_md_path)
        return 0.0, 0
    pick_date = prev_dates[-1]
    prev_picks = [r for r in pick_rows if str(r["pick_date"]) == pick_date]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict] = []
    for p in prev_picks:
        code = str(p["code"])
        eval_close = today_close_map.get(code)
        if eval_close is None:
            continue
        pick_close = float(p["pick_close"])
        pct = ((eval_close - pick_close) / pick_close * 100.0) if pick_close else 0.0
        rows.append(
            {
                "run_at": now,
                "pick_date": pick_date,
                "eval_date": trade_date,
                "code": code,
                "name": p["name"],
                "total_score": float(p["total_score"]),
                "pick_close": round(pick_close, 3),
                "eval_close": round(eval_close, 3),
                "close_vs_pick_close_pct": round(pct, 3),
                "is_win": 1 if eval_close > pick_close else 0,
            }
        )

    rows = sorted(rows, key=lambda x: float(x["total_score"]), reverse=True)
    _append_history_csv(history_csv_path, rows)
    _render_latest_md(rows, latest_md_path)

    total = len(rows)
    wins = sum(int(r["is_win"]) for r in rows)
    return ((wins / total) if total else 0.0), total

