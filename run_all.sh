#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
#  五策略顺序执行 + Parquet 预拉取 + 统一聚合
#  所有策略代码已整合到 strategies/ 目录统一管理
#  原项目保留在 ~/Desktop/ 下作为只读备份
# ─────────────────────────────────────────────────────────
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STRATEGIES_DIR="${SCRIPT_DIR}/strategies"
DATA_DIR="${SCRIPT_DIR}/data"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

TASK_INTERVAL="${TASK_INTERVAL:-10}"

# 各策略超时（秒），CI 可通过环境变量加大
TIMEOUT_QL_DRAGON="${TIMEOUT_QL_DRAGON:-180}"
TIMEOUT_ZHUSH="${TIMEOUT_ZHUSH:-150}"
TIMEOUT_PH_AFTERCLOSE="${TIMEOUT_PH_AFTERCLOSE:-180}"
TIMEOUT_LH_QUANT="${TIMEOUT_LH_QUANT:-900}"
TIMEOUT_STORAGE_IPO="${TIMEOUT_STORAGE_IPO:-120}"

# macOS 兼容: 没有 timeout 命令则用 perl 替代
if ! command -v timeout &>/dev/null; then
  timeout() {
    local duration="$1"; shift
    perl -e '
      eval {
        local $SIG{ALRM} = sub { die "timeout\n" };
        alarm shift;
        system @ARGV;
        alarm 0;
      };
      if ($@) {
        exit 142 if $@ eq "timeout\n";
        die $@;
      }
      exit $? >> 8;
    ' "$duration" "$@"
  }
fi

log()  { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo -e "${GREEN}[$(date +%H:%M:%S)] ✓${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)] ⚠${NC} $*"; }
fail() { echo -e "${RED}[$(date +%H:%M:%S)] ✗${NC} $*"; }

# ── --clear-cache 支持 ──
if [[ "${1:-}" == "--clear-cache" ]]; then
  if [[ -d "$DATA_DIR" ]]; then
    find "$DATA_DIR" -name "*.parquet" -delete
    log "Parquet 缓存已清除: $DATA_DIR"
  else
    log "无缓存文件"
  fi
  exit 0
fi

# ── Token 加载 ──
if [[ -z "${TUSHARE_TOKEN:-}" ]]; then
  if command -v security >/dev/null 2>&1; then
    KEYCHAIN_TOKEN="$(security find-generic-password -s "cursor-quant-tushare" -a "default" -w 2>/dev/null || true)"
    if [[ -n "${KEYCHAIN_TOKEN}" ]]; then
      export TUSHARE_TOKEN="${KEYCHAIN_TOKEN}"
    fi
  fi
fi
if [[ -z "${TUSHARE_TOKEN:-}" ]]; then
  # 从原项目备份 or 新位置尝试加载
  for secrets_file in \
    "$HOME/Desktop/擒龙项目_副本/.secrets/tushare_token" \
    "$HOME/Desktop/盘后扫描追随/.secrets/tushare_token" \
    "$HOME/Desktop/量化选股项目/.secrets/tushare_token" \
    "$STRATEGIES_DIR/ql_dragon/.secrets/tushare_token" \
    "$STRATEGIES_DIR/ph_afterclose/.secrets/tushare_token"; do
    if [[ -f "$secrets_file" ]]; then
      FILE_TOKEN="$(tr -d '\r\n' < "$secrets_file")"
      if [[ -n "$FILE_TOKEN" ]]; then
        export TUSHARE_TOKEN="$FILE_TOKEN"
        break
      fi
    fi
  done
fi
if [[ -z "${TUSHARE_TOKEN:-}" ]]; then
  fail "未找到 Tushare Token，请先配置"
  exit 1
fi
log "Tushare Token 已加载 (len=${#TUSHARE_TOKEN})"

# 清掉代理
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY 2>/dev/null || true
export no_proxy="*"
export NO_PROXY="*"

# ── 环境变量 ──
unset TUSHARE_CACHE_DB
export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}"
export QUANT_DATA_DIR="${SCRIPT_DIR}"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "开始 预拉取 + 五策略 执行（间隔 ${TASK_INTERVAL}s + Parquet 缓存）"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 步骤 0: 预拉取 ──────────────────────────────────────
echo ""
log "[0] 预拉取 — 刷新 trade_cal + 增量拉取 Tushare 数据到 Parquet（lookback=3 天）..."
cd "$SCRIPT_DIR"
if python3 -m quant_data.fetcher --lookback 3; then
  ok "预拉取 完成"
else
  warn "预拉取 执行失败，继续执行策略（将回退直连 Tushare）"
fi

# ── 步骤 0.5: 概念配置同步 ──────────────────────────────
echo ""
log "[0.5] 概念配置 — 同步统一 concepts.yaml → 各策略 config/themes.json..."
SYNC_TARGETS=(
  "$STRATEGIES_DIR/ql_dragon"
  "$STRATEGIES_DIR/zhush_mainrise"
  "$STRATEGIES_DIR/lh_quant"
  "$STRATEGIES_DIR/ph_afterclose"
)
SYNC_OK=0
SYNC_FAIL=0
for target in "${SYNC_TARGETS[@]}"; do
  if cd "$SCRIPT_DIR" && python3 -c "
from quant_data.concepts import write_local_themes_json
try:
    p = write_local_themes_json('$target')
    print(p)
except Exception as e:
    print(f'error: {e}')
    exit(1)
" 2>/dev/null; then
    ok "  概念同步 → ${target}"
    SYNC_OK=$((SYNC_OK+1))
  else
    warn "  概念同步失败 → ${target}"
    SYNC_FAIL=$((SYNC_FAIL+1))
  fi
done
log "概念同步: ${SYNC_OK} 成功, ${SYNC_FAIL} 失败"

PASS=0
TOTAL=5
FAILED_TASKS=()

# ── 任务 1: 擒龙猎手 ────────────────────────────────────
echo ""
log "[1/${TOTAL}] 擒龙猎手 — 造龙结构多因子扫描（${TIMEOUT_QL_DRAGON}s 超时）"
EXIT_CODE=0
cd "$STRATEGIES_DIR/ql_dragon" && timeout "$TIMEOUT_QL_DRAGON" python3 scripts/scan_dragons.py --skip-fina --skip-chip --max-analyze 30
EXIT_CODE=$?
if [[ $EXIT_CODE -eq 0 ]]; then
  ok "擒龙猎手 完成"
  PASS=$((PASS+1))
elif [[ $EXIT_CODE -eq 124 || $EXIT_CODE -eq 142 ]]; then
  warn "擒龙猎手 超时（${TIMEOUT_QL_DRAGON}s），跳过继续"
  FAILED_TASKS+=("擒龙猎手(超时)")
else
  fail "擒龙猎手 执行失败（退出码 $EXIT_CODE）"
  FAILED_TASKS+=("擒龙猎手")
fi

log "等待 ${TASK_INTERVAL}s 再执行下一策略..."
sleep "$TASK_INTERVAL"

# ── 任务 2: 主升行情启动 ────────────────────────────────
echo ""
log "[2/${TOTAL}] 主升行情启动 — 十五五主升浪扫描（${TIMEOUT_ZHUSH}s 超时）"
EXIT_CODE=0
cd "$STRATEGIES_DIR/zhush_mainrise" && timeout "$TIMEOUT_ZHUSH" python3 main.py
EXIT_CODE=$?
if [[ $EXIT_CODE -eq 0 ]]; then
  ok "主升行情启动 完成"
  PASS=$((PASS+1))
elif [[ $EXIT_CODE -eq 124 || $EXIT_CODE -eq 142 ]]; then
  warn "主升行情启动 超时（${TIMEOUT_ZHUSH}s），跳过继续"
  FAILED_TASKS+=("主升行情启动(超时)")
else
  fail "主升行情启动 执行失败（退出码 $EXIT_CODE）"
  FAILED_TASKS+=("主升行情启动")
fi

log "等待 ${TASK_INTERVAL}s 再执行下一策略..."
sleep "$TASK_INTERVAL"

# ── 任务 3: 盘后扫描追随 ────────────────────────────────
echo ""
log "[3/${TOTAL}] 盘后扫描追随 — 龙头跟随扫描（${TIMEOUT_PH_AFTERCLOSE}s 超时）"
EXIT_CODE=0
cd "$STRATEGIES_DIR/ph_afterclose" && timeout "$TIMEOUT_PH_AFTERCLOSE" bash run.sh
EXIT_CODE=$?
if [[ $EXIT_CODE -eq 0 ]]; then
  ok "盘后扫描追随 完成"
  PASS=$((PASS+1))
elif [[ $EXIT_CODE -eq 124 || $EXIT_CODE -eq 142 ]]; then
  warn "盘后扫描追随 超时（${TIMEOUT_PH_AFTERCLOSE}s），跳过继续"
  FAILED_TASKS+=("盘后扫描追随(超时)")
else
  fail "盘后扫描追随 执行失败（退出码 $EXIT_CODE）"
  FAILED_TASKS+=("盘后扫描追随")
fi

log "等待 ${TASK_INTERVAL}s 再执行下一策略..."
sleep "$TASK_INTERVAL"

# ── 任务 4: 量化蓄势突破 ────────────────────────────────
echo ""
log "[4/${TOTAL}] 量化蓄势突破 — 蓄势突破选股（${TIMEOUT_LH_QUANT}s 超时）"
SETUP_EXIT=0
cd "$STRATEGIES_DIR/lh_quant" && timeout "$TIMEOUT_LH_QUANT" bash scripts/run_daily.sh || SETUP_EXIT=$?
if [[ $SETUP_EXIT -eq 0 ]]; then
  ok "量化蓄势突破 完成"
  PASS=$((PASS+1))
elif [[ $SETUP_EXIT -eq 124 || $SETUP_EXIT -eq 142 ]]; then
  warn "量化蓄势突破 超时（${TIMEOUT_LH_QUANT}s），跳过继续"
  FAILED_TASKS+=("量化蓄势突破(超时)")
else
  fail "量化蓄势突破 执行失败（退出码 $SETUP_EXIT）"
  FAILED_TASKS+=("量化蓄势突破")
fi

log "等待 ${TASK_INTERVAL}s 再执行下一策略..."
sleep "$TASK_INTERVAL"

# ── 任务 5: 存储 IPO 供应链 ──────────────────────────────
echo ""
log "[5/${TOTAL}] 存储IPO供应链 — 长鑫/长存可上车扫描（${TIMEOUT_STORAGE_IPO}s 超时）"
EXIT_CODE=0
cd "$STRATEGIES_DIR/lh_quant" && timeout "$TIMEOUT_STORAGE_IPO" python3 storage_ipo_scan.py
EXIT_CODE=$?
if [[ $EXIT_CODE -eq 0 ]]; then
  ok "存储IPO供应链 完成"
  PASS=$((PASS+1))
elif [[ $EXIT_CODE -eq 124 || $EXIT_CODE -eq 142 ]]; then
  warn "存储IPO供应链 超时（${TIMEOUT_STORAGE_IPO}s），跳过继续"
  FAILED_TASKS+=("存储IPO供应链(超时)")
else
  fail "存储IPO供应链 执行失败（退出码 $EXIT_CODE）"
  FAILED_TASKS+=("存储IPO供应链")
fi

# ── 新鲜度自愈：若量化蓄势突破超时但后台进程仍在产出今日 CSV，轮询等待 ──
if [[ " ${FAILED_TASKS[*]} " =~ "量化蓄势突破" ]]; then
  TODAY_STR="$(date +%F)"
  LH_CSV="${STRATEGIES_DIR}/lh_quant/output/daily/daily_selection_${TODAY_STR}.csv"
  LH_WAIT_MAX="${LH_QUANT_HEAL_WAIT:-600}"
  echo ""
  log "[自愈] 量化蓄势突破超时，等待后台进程生成今日 CSV（最多${LH_WAIT_MAX}s）…"
  HEALED=0
  for ((_i=0; _i<LH_WAIT_MAX; _i+=10)); do
    if [[ -f "$LH_CSV" ]]; then
      HEALED=1; break
    fi
    sleep 10
  done
  if [[ $HEALED -eq 1 ]]; then
    ok "[自愈] 量化蓄势突破 今日 CSV 已生成 ✓"
    # 从失败列表中移除 量化蓄势突破(超时)，恢复 PASS 计数
    NEW_FAILED=()
    for _t in "${FAILED_TASKS[@]}"; do
      [[ "$_t" != 量化蓄势突破* ]] && NEW_FAILED+=("$_t")
    done
    FAILED_TASKS=("${NEW_FAILED[@]}")
    PASS=$((PASS+1))
  else
    warn "[自愈] ${LH_WAIT_MAX}s 后仍未生成今日 CSV，聚合将沿用旧数据"
  fi
fi

# ── 运行状态落盘（供聚合器在看板上显式标注本次失败/超时的策略）──
mkdir -p "${SCRIPT_DIR}/output"
_FAILED_JSON="$(printf '%s\n' "${FAILED_TASKS[@]}" | python3 -c "
import sys, json
items = [l.strip() for l in sys.stdin if l.strip()]
print(json.dumps(items, ensure_ascii=False))
")"
python3 -c "
import json, time
from pathlib import Path
status = {
    'run_ts': time.strftime('%Y-%m-%d %H:%M:%S'),
    'run_date': time.strftime('%Y%m%d'),
    'pass': ${PASS},
    'total': ${TOTAL},
    'failed': json.loads('''${_FAILED_JSON}'''),
}
Path('${SCRIPT_DIR}/output/.run_status.json').write_text(
    json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8'
)
print(f'  运行状态已记录: {status[\"pass\"]}/{status[\"total\"]} 成功, 失败 {len(status[\"failed\"])}')
" 2>/dev/null || true

# ── 缓存统计 ──
echo ""
python3 -c "
from quant_data import get_provider
pro = get_provider()
pro.print_stats()
" 2>/dev/null || true

# ── 统一聚合 ────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "[聚合] 刷新交易日历 + 补齐最近 5 个交易日 daily/moneyflow..."
cd "$SCRIPT_DIR"
python3 -c "
from pathlib import Path
from quant_data.fetcher import _get_pro, ensure_recent_market_partitions

data_dir = Path('$SCRIPT_DIR/data')
pro = _get_pro()
last_open = ensure_recent_market_partitions(pro, data_dir=data_dir, lookback_open_days=5)
print(f'  最近开市日: {last_open}')
" 2>&1 || warn "daily/moneyflow 拉取失败，使用已有缓存"

log "[聚合] 开始统一聚合..."
cd "$SCRIPT_DIR"
if python3 aggregate.py; then
  ok "统一聚合 完成"
else
  fail "统一聚合 执行失败"
  exit 1
fi

# ── 汇总 ────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ ${#FAILED_TASKS[@]} -eq 0 ]]; then
  ok "全部完成！${PASS}/${TOTAL} 策略成功"
else
  warn "完成，但 ${#FAILED_TASKS[@]} 个策略失败: ${FAILED_TASKS[*]}"
fi
echo ""
log "输出文件:"
log "  HTML 看板: ${SCRIPT_DIR}/output/unified_dashboard.html"
log "  微信文本: ${SCRIPT_DIR}/output/unified_wechat.txt"
log "  汇总 CSV: ${SCRIPT_DIR}/output/unified_all_picks.csv"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
