#!/usr/bin/env python3
"""单票深度分析：基本面、走势、技术面、箱体、持仓/空仓策略。

Tushare 经 ``qinlong.tushare_client.get_pro_api()``，与扫描脚本共用
``qinlong.secrets.get_tushare_token``（环境变量 → 钥匙串 → 仓库根 ``.secrets/tushare_token``）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qinlong.single_stock_analysis import build_text_report, fetch_analysis_bundle, resolve_ts_code
from qinlong.tushare_client import get_pro_api


def main() -> int:
    p = argparse.ArgumentParser(
        description="单票基本面+走势+技术+箱体+策略（Tushare）。",
        epilog="Token：与 scan_dragons 相同，使用 qinlong.secrets（TUSHARE_TOKEN → 钥匙串 → 仓库根 .secrets/tushare_token）。",
    )
    p.add_argument(
        "query",
        nargs="?",
        default=None,
        help="股票代码（如 603399 / 603399.SH）或中文简称关键字（如 永杉锂业）",
    )
    p.add_argument("--code", default=None, help="等价于 positional query，二选一")
    p.add_argument("--trade-date", default=None, help="YYYYMMDD；省略则用最近开市日")
    p.add_argument("--lookback", type=int, default=550, help="日线向前取数的自然日跨度（默认 550）")
    p.add_argument("--box-window", type=int, default=60, help="箱体统计用的最近K线根数（默认 60）")
    p.add_argument(
        "--sideways-max-pct",
        type=float,
        default=2.5,
        help="横盘判定：|涨跌幅|≤该值(%%)（默认 2.5）",
    )
    p.add_argument(
        "--sideways-max-amp",
        type=float,
        default=4.0,
        help="横盘判定：振幅(高-低)/昨收×100≤该值(%%)（默认 4.0）",
    )
    p.add_argument(
        "--sideways-scan",
        type=int,
        default=120,
        help="统计「最长连续横盘」时向前看的K线根数（默认 120）",
    )
    p.add_argument("--fina-rows", type=int, default=6, help="财报展示最近期数（默认 6）")
    p.add_argument("-o", "--output", default=None, help="写入 Markdown 文件路径（UTF-8）；省略则打印到 stdout")
    args = p.parse_args()

    q = args.code or args.query
    if not q:
        p.error("请提供股票：analyze_stock.py 永杉锂业 或 --code 603399.SH")

    pro = get_pro_api(timeout=120)
    ts, err = resolve_ts_code(pro, q)
    if err or not ts:
        print(err or "无法解析代码", file=sys.stderr)
        return 1

    bundle = fetch_analysis_bundle(
        pro,
        ts,
        trade_date=args.trade_date,
        lookback_calendar_days=args.lookback,
        fina_rows=args.fina_rows,
    )
    text = build_text_report(
        bundle,
        box_window=args.box_window,
        sideways_max_abs_pct_chg=args.sideways_max_pct,
        sideways_max_amplitude_pct=args.sideways_max_amp,
        sideways_scan_window=args.sideways_scan,
    )

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"written: {out}")
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
