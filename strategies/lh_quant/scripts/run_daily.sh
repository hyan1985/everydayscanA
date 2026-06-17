#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${PROJECT_ROOT}"

# 让 quant_data 可导入（统一运行模式下由 run_all.sh 设置 QUANT_DATA_DIR）
_QUANT_DIR="${QUANT_DATA_DIR:-$HOME/Desktop/统一输出}"
if [[ -d "$_QUANT_DIR" ]]; then
  export PYTHONPATH="${_QUANT_DIR}:${PYTHONPATH:-}"
fi

TOP_N="${1:-8}"
OUT_DIR="${PROJECT_ROOT}/output"
DAILY_DIR="${OUT_DIR}/daily"
JOURNAL_DIR="${OUT_DIR}/journal"
DASHBOARD_DIR="${OUT_DIR}/dashboard"
BOX_DIR="${OUT_DIR}/box_range_monitor"
mkdir -p "${OUT_DIR}" "${DAILY_DIR}" "${JOURNAL_DIR}" "${DASHBOARD_DIR}"

# token 读取优先级：
# 1) 环境变量 TUSHARE_TOKEN
# 2) macOS 钥匙串服务 cursor-quant-tushare / 账号 default
# 3) 本地文件 .secrets/tushare_token（建议 chmod 600）
if [[ -z "${TUSHARE_TOKEN:-}" ]]; then
  if command -v security >/dev/null 2>&1; then
    KEYCHAIN_TOKEN="$(security find-generic-password -s "cursor-quant-tushare" -a "default" -w 2>/dev/null || true)"
    if [[ -n "${KEYCHAIN_TOKEN}" ]]; then
      export TUSHARE_TOKEN="${KEYCHAIN_TOKEN}"
    fi
  fi
fi

if [[ -z "${TUSHARE_TOKEN:-}" && -f ".secrets/tushare_token" ]]; then
  FILE_TOKEN="$(tr -d '\r\n' < ".secrets/tushare_token")"
  if [[ -n "${FILE_TOKEN}" ]]; then
    export TUSHARE_TOKEN="${FILE_TOKEN}"
  fi
fi

if [[ -z "${TUSHARE_TOKEN:-}" ]]; then
  echo "Error: 未找到 Tushare Token。"
  echo "请先执行: ./scripts/set_tushare_token.sh \"你的token\""
  echo "或临时设置: export TUSHARE_TOKEN=\"你的token\""
  exit 1
fi

TODAY="$(date +%F)"
OUT_CSV="${DAILY_DIR}/daily_selection_${TODAY}.csv"

export TOP_N
export OUT_CSV

echo "[Precheck] Tushare token loaded: YES (len=${#TUSHARE_TOKEN})"

# 主题单次扫描且跳过后续任务时，不写默认 OUT_CSV，避免用窄结果覆盖今日主线路径。
if [[ "${QUANT_SKIP_POST_TASKS:-}" == "1" ]]; then
  export OUT_CSV=""
fi

python3 - <<'PY'
import os
import pandas as pd
from datetime import datetime
from pathlib import Path

from searchv1 import run_daily_selection, to_chinese_columns

top_n = int(os.getenv("TOP_N", "8"))
token = os.getenv("TUSHARE_TOKEN", "").strip()
out_csv = os.getenv("OUT_CSV", "").strip()

df = run_daily_selection(token=token, top_n=top_n)
if out_csv:
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    if df is None or df.empty:
        # 空结果也要写表头，防止下游 csv 读取崩溃
        df_empty = pd.DataFrame(columns=["ts_code", "name", "industry", "score_norm",
            "pool_type", "strategy", "strategy_short", "latest_trade_date"])
        df_empty.to_csv(out_csv, index=False, encoding="utf-8-sig")
        cn_csv = out_csv.replace(".csv", "_cn.csv")
        df_empty.rename(columns={"ts_code": "股票代码", "name": "股票名称",
            "industry": "行业", "score_norm": "归一化得分"}).to_csv(cn_csv, index=False, encoding="utf-8-sig")
        print(f"\n⚠ 当日双池无候选，已写入空表头 CSV")
        print(f"已导出: {out_csv} (0行)")
        print(f"中文字段导出: {cn_csv} (0行)")
    else:
        df.to_csv(out_csv, index=False, encoding="utf-8-sig")
        cn_csv = out_csv.replace(".csv", "_cn.csv")
        to_chinese_columns(df).to_csv(cn_csv, index=False, encoding="utf-8-sig")
        print(f"\n已导出: {out_csv}")
        print(f"中文字段导出: {cn_csv}")
PY

if [[ "${QUANT_SKIP_POST_TASKS:-}" != "1" ]]; then
  python3 "update_tracking_register.py" --input "${OUT_CSV}" --register "${JOURNAL_DIR}/tracking_register.csv" || echo "[skip] update_tracking_register"
  python3 "update_signal_journal.py" --input "${OUT_CSV}" --journal "${JOURNAL_DIR}/signal_journal.csv" --manual "${JOURNAL_DIR}/manual_trade_inputs.csv" --top-k 3 || echo "[skip] update_signal_journal"

  python3 "build_dashboard.py" --output-dir "${OUT_DIR}" --top-k 20 --max-kline 15 || echo "[skip] build_dashboard"
else
  echo "[Skip] QUANT_SKIP_POST_TASKS=1：未更新 tracking / journal / dashboard。"
fi

echo "完成。"
