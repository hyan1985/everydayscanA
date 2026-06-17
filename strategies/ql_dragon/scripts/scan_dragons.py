#!/usr/bin/env python3
"""造龙风格扫描 CLI：题材 × 资金 × 技术 × 筹码/基本面。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# 支持从项目根目录直接运行
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qinlong.scanner.pipeline import DragonScanner
from qinlong.dashboard_export import export_from_scan_csv
from qinlong.html_dashboard import write_dashboard_html
from qinlong.tushare_client import get_pro_api


def main() -> int:
    p = argparse.ArgumentParser(
        description="A 股「造龙结构」多因子粗筛（研究用途）。"
        "默认启用接口级节流：stk_factor_pro(5000 分约 30 次/分)、全局限流、"
        "hm_detail 默认不拉取（约 2 次/小时）。"
    )
    p.add_argument("--trade-date", default=None, help="YYYYMMDD；省略则用最近开市日")
    p.add_argument("--top-concepts", type=int, default=12, help="同花顺热榜板块数量上限")
    p.add_argument("--max-analyze", type=int, default=40, help="最多深度打分股票数量（控制总耗时）")
    p.add_argument(
        "--points",
        type=int,
        default=5000,
        help="Tushare 积分档位；>=8000 时 stk_factor 使用更短间隔，否则用 2.05s/次",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="每次请求完成后**额外**等待秒数（在节流器之后叠加，建议网络不稳时 0.05~0.2）",
    )
    p.add_argument(
        "--with-hm",
        action="store_true",
        help="拉取龙虎榜 hm_detail（频控极严，约 2 次/小时；未加此开关则默认跳过）",
    )
    p.add_argument(
        "--open-dashboard",
        action="store_true",
        help="跑完后尝试在 macOS 上打开仪表盘 Canvas 文件（优先用 Cursor 打开）",
    )
    p.add_argument(
        "--html",
        default=str(_ROOT / "reports/dashboard.html"),
        help="静态 HTML 仪表盘输出路径（默认 reports/dashboard.html）",
    )
    p.add_argument(
        "--open-html",
        action="store_true",
        help="跑完后在 macOS 上自动打开静态 HTML 仪表盘",
    )
    p.add_argument(
        "--open",
        action="store_true",
        help="等价于 --open-html（保留短参数，方便日常使用）",
    )
    p.add_argument("--skip-fina", action="store_true", help="跳过财务接口（提速）")
    p.add_argument("--skip-chip", action="store_true", help="跳过筹码接口（提速）")
    p.add_argument(
        "--csv",
        default=str(_ROOT / "reports/latest_scan.csv"),
        help="导出 CSV 路径（默认 reports/latest_scan.csv，并用于自动刷新仪表盘）",
    )
    args = p.parse_args()

    pro = get_pro_api(timeout=120)
    scanner = DragonScanner(pro, trade_date=args.trade_date)
    df, debug = scanner.run(
        top_concepts=args.top_concepts,
        max_analyze=args.max_analyze,
        points_tier=args.points,
        extra_sleep=args.sleep,
        skip_fina=args.skip_fina,
        skip_chip=args.skip_chip,
        skip_hm_detail=not args.with_hm,
    )

    print("trade_date:", debug.get("trade_date"))
    print("throttle:", debug.get("throttle"))
    print("candidates_debug:", debug.get("candidates"))
    print("rows:", debug.get("rows"))
    if df.empty:
        print("无输出（候选为空或被过滤）。")
        return 1

    cols = [
        "score",
        "ts_code",
        "name",
        "industry",
        "s_theme",
        "s_news",
        "s_technical",
        "s_cap_turnover",
        "s_fundamental",
        "s_chip",
        "best_concept_rank",
        "from_limit_list",
        "from_hm_detail",
        "limit_up_days",
        "turnover_rate",
        "circ_mv_yi",
        "close",
        "ema_5",
        "close_to_prev_high",
        "vol_to_ma5",
        "concept_tags",
    ]
    view = df[[c for c in cols if c in df.columns]]
    with pd_option():
        print(view.to_string(index=False))

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False)
        print("written:", str(out))
        try:
            canvas_path = export_from_scan_csv(out, debug.get("trade_date"))
            if canvas_path is not None:
                print("dashboard_updated:", str(canvas_path))
                if args.open_dashboard:
                    # 尝试用 Cursor 打开；失败则退化为系统默认打开方式
                    import subprocess

                    try:
                        subprocess.run(["open", "-a", "Cursor", str(canvas_path)], check=False)
                    except Exception:
                        try:
                            subprocess.run(["open", str(canvas_path)], check=False)
                        except Exception:
                            pass
        except Exception as exc:
            print("dashboard_update_failed:", str(exc))

    # 静态 HTML 仪表盘（离线可分享）
    try:
        html_path = write_dashboard_html(
            df,
            Path(args.html),
            trade_date=str(debug.get("trade_date") or ""),
            throttle=debug.get("throttle"),
        )
        print("html_written:", str(html_path))
        if args.open_html or args.open:
            import subprocess

            try:
                subprocess.run(["open", str(html_path)], check=False)
            except Exception:
                pass
    except Exception as exc:
        print("html_failed:", str(exc))
    return 0


class pd_option:
    """pandas 显示宽度上下文。"""

    def __enter__(self):
        self._pd = pd
        self._mw = pd.get_option("display.max_colwidth")
        pd.set_option("display.max_colwidth", 60)
        pd.set_option("display.width", 200)
        return self

    def __exit__(self, *exc):
        self._pd.set_option("display.max_colwidth", self._mw)
        return False


if __name__ == "__main__":
    raise SystemExit(main())
