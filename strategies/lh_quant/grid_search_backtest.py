"""
参数网格回测：用于寻找更稳健的风险参数组合
"""

import argparse
import itertools
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from backtest_strategy import RiskControlConfig, run_backtest


def parse_float_list(raw: str):
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def composite_score(summary: dict) -> float:
    # 简单稳健评分：偏好更高夏普、更高胜率、更低回撤
    sharpe = summary.get("sharpe_monthly") or 0.0
    win = summary.get("strategy_win_rate") or 0.0
    dd = abs(summary.get("max_drawdown") or 0.0)
    annual = summary.get("annual_return") or 0.0
    return annual + 0.15 * sharpe + 0.2 * win - 0.25 * dd


def main():
    p = argparse.ArgumentParser(description="风险参数网格回测")
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--top-n", type=int, default=8)
    p.add_argument("--token", type=str, default="", help="可选，默认从环境变量读取")
    p.add_argument("--stop-loss-grid", type=str, default="0.06,0.08,0.10")
    p.add_argument("--industry-cap-grid", type=str, default="0.25,0.35,0.45")
    p.add_argument("--max-vol-grid", type=str, default="0.45,0.50,0.55")
    args = p.parse_args()

    token = (args.token or os.getenv("TUSHARE_TOKEN", "")).strip()
    if not token:
        raise ValueError("请设置 TUSHARE_TOKEN 或通过 --token 传入。")

    stop_losses = parse_float_list(args.stop_loss_grid)
    industry_caps = parse_float_list(args.industry_cap_grid)
    max_vols = parse_float_list(args.max_vol_grid)

    combinations = list(itertools.product(stop_losses, industry_caps, max_vols))
    print(f"网格组合数: {len(combinations)}")

    rows = []
    for idx, (sl, icap, mvol) in enumerate(combinations, 1):
        print(f"\n[{idx}/{len(combinations)}] stop_loss={sl:.2%}, industry_cap={icap:.0%}, max_vol={mvol:.2f}")
        risk_cfg = RiskControlConfig(
            max_volatility_30d=mvol,
            min_breakout_readiness=55.0,
            min_turnover_rate=0.8,
            max_turnover_rate=10.0,
            max_industry_ratio=icap,
            stop_loss_pct=sl,
        )
        bt = run_backtest(
            token=token,
            years=args.years,
            top_n=args.top_n,
            rebalance_freq="M",
            risk_cfg=risk_cfg,
            save_output=False,
        )
        s = bt.attrs.get("summary", {})
        row = {
            "stop_loss_pct": sl,
            "max_industry_ratio": icap,
            "max_volatility_30d": mvol,
            "annual_return": s.get("annual_return"),
            "strategy_win_rate": s.get("strategy_win_rate"),
            "excess_win_rate": s.get("excess_win_rate"),
            "max_drawdown": s.get("max_drawdown"),
            "sharpe_monthly": s.get("sharpe_monthly"),
            "score": composite_score(s),
        }
        rows.append(row)

    df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    out_dir = Path("output") / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file = out_dir / f"grid_search_{args.years}y_{stamp}.csv"
    json_file = out_dir / f"grid_search_best_{args.years}y_{stamp}.json"
    df.to_csv(csv_file, index=False, encoding="utf-8-sig")
    best = df.iloc[0].to_dict()
    json_file.write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n==== 网格回测Top 5 ====")
    print(df.head(5))
    print(f"\n结果输出: {csv_file}")
    print(f"最优参数: {json_file}")


if __name__ == "__main__":
    main()
