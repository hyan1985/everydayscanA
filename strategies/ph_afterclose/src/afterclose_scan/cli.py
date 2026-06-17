from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from .models import MarketSnapshot, SectorSnapshot, StockSnapshot
from .performance import track_daily_performance
from .reporting import render_report
from .scoring import evaluate_emotion, pick_top_sectors, score_stock
from .static_dashboard import render_static_dashboard
from .tushare_client import TushareClient


def _load_input(path: Path) -> tuple[MarketSnapshot, list[SectorSnapshot], list[StockSnapshot]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    market = MarketSnapshot(**data["market"])
    sectors = [SectorSnapshot(**i) for i in data["sectors"]]
    stocks = [StockSnapshot(**i) for i in data["stocks"]]
    return market, sectors, stocks


def _generate_report(input_path: Path, output_path: Path, minimal_n: int = 5) -> list:
    market, sectors, stocks = _load_input(input_path)
    emotion, position_advice, principle = evaluate_emotion(market)
    top_sectors = pick_top_sectors(sectors, n=3)
    strongest = {s.name for s in top_sectors}
    scores = sorted(
        [score_stock(stock, strongest_sectors=strongest) for stock in stocks],
        key=lambda x: x.total_score,
        reverse=True,
    )
    report = render_report(
        emotion=emotion,
        position_advice=position_advice,
        principle=principle,
        top_sectors=top_sectors,
        scores=scores,
        minimal_n=minimal_n,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"报告已生成: {output_path}")
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description="盘后扫描追随：基于收盘数据生成次日跟随计划")
    parser.add_argument("--input", help="输入 JSON 路径")
    parser.add_argument("--output", help="输出报告路径 (md)")
    parser.add_argument(
        "--static-html",
        help="生成静态仪表盘HTML路径",
    )
    parser.add_argument(
        "--check-tushare",
        action="store_true",
        help="检查 Tushare Token 是否可用（从环境变量或 Keychain 读取）",
    )
    parser.add_argument(
        "--bootstrap-keychain",
        metavar="TOKEN",
        help="将 Tushare Token 写入 Keychain（service=tushare_token）",
    )
    parser.add_argument(
        "--auto-scan",
        action="store_true",
        help="自动从 Tushare 拉取数据并生成盘后输入+报告",
    )
    parser.add_argument(
        "--auto-input",
        default="data/input.auto.json",
        help="自动扫描生成的输入 JSON 路径",
    )
    parser.add_argument(
        "--auto-output",
        default="reports/auto.md",
        help="自动扫描生成的报告路径",
    )
    parser.add_argument(
        "--auto-top-n",
        type=int,
        default=12,
        help="自动扫描候选股上限（默认12）",
    )
    parser.add_argument(
        "--auto-hot-sectors",
        type=int,
        default=6,
        help="自动扫描热门板块数量（默认6）",
    )
    parser.add_argument(
        "--auto-per-sector",
        type=int,
        default=4,
        help="每个热门板块保留前排票数量（默认4）",
    )
    parser.add_argument(
        "--minimal-n",
        type=int,
        default=5,
        help="重点关注与执行清单条数（默认5）",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="回溯最近N个交易日并补齐次日胜率历史（默认0不回溯）",
    )
    parser.add_argument(
        "--sector-mode",
        choices=("ths_concept", "industry"),
        default=(os.getenv("SECTOR_MODE") or "ths_concept"),
        help="板块口径：ths_concept=同花顺概念(需权限)，industry=申万行业；概念失败会自动回退行业",
    )
    args = parser.parse_args()

    if args.bootstrap_keychain:
        account = os.getenv("USER", "default")
        cmd = [
            "security",
            "add-generic-password",
            "-a",
            account,
            "-s",
            "tushare_token",
            "-w",
            args.bootstrap_keychain,
            "-U",
        ]
        subprocess.run(cmd, check=True)
        print("已写入 Keychain: service=tushare_token")
        return

    if args.check_tushare:
        ok = TushareClient.from_secure_config().check_connection()
        print("Tushare 连通性: OK" if ok else "Tushare 连通性: FAILED")
        raise SystemExit(0 if ok else 1)

    if args.auto_scan:
        from .automation import build_auto_input

        if args.backfill_days > 0:
            pro = TushareClient.from_secure_config().pro()
            cal = pro.trade_cal(
                exchange="",
                start_date="20240101",
                end_date=datetime.now().strftime("%Y%m%d"),
                is_open=1,
            )
            if cal is None or cal.empty:
                raise RuntimeError("无法获取交易日历，回溯失败。")
            dates = sorted(cal["cal_date"].astype(str).tolist())
            selected_dates = dates[-max(2, args.backfill_days) :]
            for d in selected_dates:
                bf_input = build_auto_input(
                    Path(f"data/backfill/input_{d}.json"),
                    top_n=max(5, args.auto_top_n),
                    hot_sector_n=max(1, args.auto_hot_sectors),
                    per_sector_n=max(1, args.auto_per_sector),
                    target_trade_date=d,
                    sector_mode=args.sector_mode,
                )
                bf_scores = _generate_report(
                    bf_input,
                    Path(f"reports/backfill/{d}.md"),
                    minimal_n=max(1, args.minimal_n),
                )
                try:
                    track_daily_performance(input_path=bf_input, scores=bf_scores)
                except Exception:
                    pass
            print(f"历史回溯完成: {len(selected_dates)} 个交易日")

        input_path = build_auto_input(
            Path(args.auto_input),
            top_n=max(5, args.auto_top_n),
            hot_sector_n=max(1, args.auto_hot_sectors),
            per_sector_n=max(1, args.auto_per_sector),
            sector_mode=args.sector_mode,
        )
        print(f"自动输入已生成: {input_path}")
        scores = _generate_report(input_path, Path(args.auto_output), minimal_n=max(1, args.minimal_n))
        try:
            win_rate, sample_n = track_daily_performance(input_path=input_path, scores=scores)
            print(f"当日胜率跟踪已更新: 样本{sample_n} 胜率{win_rate:.1%}")
        except Exception as exc:
            print(f"当日胜率跟踪跳过: {exc}")
        if args.static_html:
            out_html = render_static_dashboard(
                input_path,
                Path(args.static_html),
                minimal_n=max(1, args.minimal_n),
            )
            print(f"静态仪表盘已生成: {out_html}")
        return

    if not args.input or not args.output:
        parser.error("生成报告模式下必须提供 --input 和 --output")

    _generate_report(Path(args.input), Path(args.output), minimal_n=max(1, args.minimal_n))
    if args.static_html:
        out_html = render_static_dashboard(
            Path(args.input),
            Path(args.static_html),
            minimal_n=max(1, args.minimal_n),
        )
        print(f"静态仪表盘已生成: {out_html}")


if __name__ == "__main__":
    main()
