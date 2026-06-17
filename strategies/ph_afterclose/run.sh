#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

# 让 quant_data 可导入（从统一输出目录或 PYTHONPATH）
_QUANT_DIR="${QUANT_DATA_DIR:-$HOME/Desktop/统一输出}"
if [[ -d "$_QUANT_DIR" ]]; then
  export PYTHONPATH="${_QUANT_DIR}:${PYTHONPATH:-}"
fi

# token 优先级:
# 1) 环境变量 TUSHARE_TOKEN
# 2) macOS 钥匙串 service=cursor-quant-tushare account=default
# 3) 本地文件 .secrets/tushare_token
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
  exit 1
fi
echo "Tushare token loaded: YES (len=${#TUSHARE_TOKEN})"

# 清掉本地代理，避免拦截 Tushare 请求
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export no_proxy="*"
export NO_PROXY="*"

TOP_N="${TOP_N:-12}"
HOT_SECTORS="${HOT_SECTORS:-6}"
PER_SECTOR="${PER_SECTOR:-4}"
MINIMAL_N="${MINIMAL_N:-5}"
SECTOR_MODE="${SECTOR_MODE:-ths_concept}"

MODE="${1:-afterclose}"
# 支持模式：
#   afterclose  -> 盘后报告 + 静态仪表盘（默认）
#   report      -> 仅生成 markdown 报告
#   nodash      -> report 别名

case "$MODE" in
  afterclose)
    STATIC_FLAG="--static-html reports/auto_dashboard.html"
    ;;
  report|nodash)
    STATIC_FLAG=""
    ;;
  *)
    echo "Unknown mode: $MODE"
    echo "Usage: ./run.sh [afterclose|report]"
    exit 1
    ;;
esac

BASE_CMD=(
  python3 -m src.afterclose_scan.cli --auto-scan
  --auto-top-n "$TOP_N"
  --auto-hot-sectors "$HOT_SECTORS"
  --auto-per-sector "$PER_SECTOR"
  --minimal-n "$MINIMAL_N"
  --sector-mode "$SECTOR_MODE"
)

if [[ -n "$STATIC_FLAG" ]]; then
  BASE_CMD+=($STATIC_FLAG)
fi

echo "[run.sh] 执行命令: ${BASE_CMD[*]}"
EXIT_CODE=0
"${BASE_CMD[@]}" || EXIT_CODE=$?
echo "[run.sh] 策略退出码: ${EXIT_CODE}（非零=失败/跳过）"
# 关键：把真实退出码透传给上层 run_all.sh，否则失败会被当成成功
exit "${EXIT_CODE}"
