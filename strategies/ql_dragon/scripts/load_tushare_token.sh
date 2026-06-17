#!/usr/bin/env bash
# Tushare token 读取优先级（与 qinlong/secrets.py 一致）：
# 1) 环境变量 TUSHARE_TOKEN
# 2) macOS 钥匙串 cursor-quant-tushare / 账号 default
# 3) 项目根下 .secrets/tushare_token（建议 chmod 600）
#
# 用法（在项目根执行，或从任意目录 source 本文件）：
#   source scripts/load_tushare_token.sh

set -u

_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

if [[ -z "${TUSHARE_TOKEN:-}" ]]; then
  if command -v security >/dev/null 2>&1; then
    KEYCHAIN_TOKEN="$(security find-generic-password -s "cursor-quant-tushare" -a "default" -w 2>/dev/null || true)"
    if [[ -n "${KEYCHAIN_TOKEN}" ]]; then
      export TUSHARE_TOKEN="${KEYCHAIN_TOKEN}"
    fi
  fi
fi

if [[ -z "${TUSHARE_TOKEN:-}" && -f "${_root}/.secrets/tushare_token" ]]; then
  FILE_TOKEN="$(tr -d '\r\n' < "${_root}/.secrets/tushare_token")"
  if [[ -n "${FILE_TOKEN}" ]]; then
    export TUSHARE_TOKEN="${FILE_TOKEN}"
  fi
fi

unset _root
