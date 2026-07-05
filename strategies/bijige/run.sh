#!/usr/bin/env bash
# 笔记哥选股 — Parquet 数据源版本
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PICKER_DIR="${ROOT_DIR}/bijige-stock-picker"
OUTPUT_CSV="${SCRIPT_DIR}/output/picks.csv"

mkdir -p "$(dirname "$OUTPUT_CSV")"
cd "$ROOT_DIR"

python3 "${PICKER_DIR}/pick.py" \
  --data-dir "${ROOT_DIR}/data" \
  --top 50 \
  --output-csv "${OUTPUT_CSV}" \
  --full \
  "$@"
