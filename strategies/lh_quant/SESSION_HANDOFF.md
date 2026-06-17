# 会话重点信息（Handoff）

最后更新时间：2026-04-27（v0.6 dual_engine 死刑判决 + v4_pure 单引擎锁定）

## 当前策略状态（已切换）

- 策略模式：`v4_pure`（**已从 dual_engine 撤回**，详见下文「2026-04-27 重大决策」）
  - 配置文件：`config/strategy.json` → `{"mode": "v4_pure"}`
  - hybrid 引擎已被数据证明在 bull 市灾难性表现，**不要再切回 dual_engine 或 hybrid**
- 股票池：主板 + `config/themes.json` 主题概念（含别名/模糊匹配）
- 日选股脚本：`scripts/run_daily.sh`
- 回测脚本（5天持有）：`scripts/run_backtest.sh <years> <top_n> <hold_days>`

## 本次重建思路（偏稳健）

- 先过滤追高与弱趋势，再做评分排序。
- 单日推荐数量下调到 4，只保留更高把握度标的。
- 估值权重上调（5年主、3年确认），降低纯技术噪声影响。
- 交易执行改为更快风控：止损更紧、止盈分级更保守。

## 本次已更新配置

- `config/strategy.json`
  - `mode: dual_engine`
- `config/risk.json`
  - 更严格追高过滤：`max_runup_10d_pct=10.0`、`max_near_high_20d_ratio=0.965`
  - 趋势/筹码过滤增强：`min_ma20_slope_20d=0.2`、`min_overhead_supply_ratio=0.88`
  - 蓄势质量提升：`min_accumulation_score=65.0`、`min_consolidation_breakout_score=62.0`
- `config/valuation.json`
  - 估值参与候选收紧：`valuation_candidate_top_n=60`
  - 估值权重提高：`0.18 + 0.12`（5y + 3y）
- `config/trade.json`
  - `max_daily_picks=4`
  - `entry_breakout_buffer_pct=0.2`
  - `entry_min_ma5_slope_5d=0.15`
  - `hard_stop_loss_pct=0.05`、`tp1_pct=0.06`、`tp2_pct=0.12`

## 你当前最关心的执行表

- 每日选股结果：`output/daily/daily_selection_YYYY-MM-DD.csv`
- 跟踪登记表（主操作表）：`output/journal/tracking_register.csv`
  - 关键字段：
    - `base_price`（首次入表基准价）
    - `tracking_days`（跟踪天数）
    - `last_price`（当日价格）
    - `return_pct` / `win_judgement`（胜负判断）
    - `total_score`（当前评分）
    - `concept_score_rank` / `concept_stock_count`（板块内排名）

## 每日最短操作流程

1. 运行：`./scripts/run_daily.sh 4`
2. 查看：`output/journal/tracking_register.csv`
3. 优先看：`total_score + concept_score_rank + win_judgement`

## 快速回测建议

- 先做近三年快测：`./scripts/run_backtest.sh 3 4 5`
- 再做五年稳健检验：`./scripts/run_backtest.sh 5 4 5`
- 若信号过少：先放宽 `min_consolidation_breakout_score` 到 `58~60`
- 若回撤偏大：继续收紧 `max_daily_picks` 到 `3`

---

## 2026-04-27 重大决策：dual_engine → v4_pure 单引擎

### 触发原因

发现 `backtest_strategy.py` 里两个 bug，修复后跑了三引擎对照回测，**直接颠覆了 dual_engine 的设计前提**。

### 已修复的 bug（已写入 backtest_strategy.py）

1. **`pick_with_industry_cap` 永远返回空列表**：原实现用 `getattr(row, "ts_code", None)` 取股票代码，但 `ts_code` 是 DataFrame 的 index 而不是 column，所以始终返回 `None`，导致整个函数失效。回测每期被迫只选 1 只（fallback）。已改为同时支持 column 和 index 两种取值方式。
2. **regime 选股逻辑硬编码 1 只**：`bear` 用 `pref[:1]`，`bull/range` 不补齐。已改为以偏好为种子、按 `total_score` 顺序补齐到 regime 目标（bull→cap×1.0，range→×0.75，bear→×0.50）。
3. **影响范围**：仅 `backtest_strategy.py`，**`searchv1.py`（实盘日选）不受影响**——历次实盘选股仍是分散持仓的，没有被 bug 污染。

### 三引擎 3 年回测对比（同区间 2022-08-11 ~ 2026-04-27，163 期，top_n=5，5 日持仓）

| 引擎 | 胜率 | 年化 | Sharpe | 最大回撤 |
|---|---:|---:|---:|---:|
| dual_engine（bug 前）| 44.17% | -11.79% | -0.23 | -46.35% |
| v4_pure 单跑（bug 前）| 51.53% | +8.02% | 0.42 | -26.86% |
| hybrid 单跑（bug 前）| 39.88% | -20.65% | -0.56 | -60.60% |
| **v4_pure 单跑（bug 修复后）** | **54.60%** | **+12.52%** | **+0.66** | **-21.59%** |
| hybrid 单跑（bug 修复后）| 39.88% | **-19.35%** | -0.77 | **-64.31%** |
| 基准（指数）| — | +10.07% | — | — |

### 按 regime × 引擎拆解（核心证据）

| Regime | 时间占比 | hybrid 胜率/单期/累积log | v4_pure 胜率/单期/累积log | hybrid - v4_pure 单期差 |
|---|---:|---|---|---:|
| **bull** | 48.5% | 36.7% / -0.82% / **-0.696** | **59.5%** / +0.53% / **+0.391** | **-1.35%**（累积差 -1.07 log）|
| range | 9.8% | 68.8% / +0.92% / +0.140 | 62.5% / +0.74% / +0.108 | +0.18%（小赢） |
| bear | 41.7% | 36.8% / -0.16% / -0.145 | 47.1% / -0.12% / -0.115 | -0.04%（半斤八两） |

163 期同期 head-to-head：hybrid 仅 42.3% 跑赢 v4_pure。

### 三个核心结论

1. **dual_engine 的设计逻辑反了**：原意「bull 市切到 hybrid 赚趋势」，实际 hybrid 在 bull 市最差（-0.696 log），把 v4_pure 的 +0.391 log 替换掉，**单 bull 一段就挥霍掉 ~1.07 log 累积复利（约 -65% 净值）**。
2. **v4_pure 才是真正的全市场策略**：bull 跑得最好、range 也最好、bear 不亏太多。hybrid 唯一赢的 range 只占 9.8% 时间，远不足以补偿 bull 损失。
3. **真正的失血点是 bear 市**（41.7% 时间，v4_pure 累积 -0.115 log），不是引擎选错。换引擎也没用——hybrid 在 bear 同样亏。

### 真正的"不同市态用不同策略"应该是

**不切引擎，切参数**——保留 v4_pure 单引擎作为底座，按 regime 调三件事（仓位/picks/门槛）：

| Regime | 引擎 | 总仓位上限 | max_daily_picks | 入场分阈值 | 备注 |
|---|---|---:|---:|---:|---|
| bull | v4_pure | 100% | 5 | ≥35 | 重仓做多 |
| range | v4_pure | 75% | 3 | ≥40 | 中等仓位 |
| bear | v4_pure | **30-50%** | **2** | **≥50** | 严守，少做精做 |
| 极端 bear（连续 N 期 ≥X% 亏损）| v4_pure | **0%** | 0 | — | risk-off 强制空仓 |

### 已完成

- 2026-04-27 23:00 起 `config/strategy.json` 锁定 `{"mode": "v4_pure"}`
- bug 修复已 commit 到 `backtest_strategy.py`（`pick_with_industry_cap` + regime 选股逻辑）
- P1 ATR 动态止损配置已加入 `config/trade.json` 的 `v4_overrides`，**但 searchv1.py 还未读取**，实盘止损仍按固定 4.5-5% 计算
- 诊断脚本：`scripts/winrate_diagnose.py`（含 bootstrap CI、McNemar、stationary bootstrap）

### 待办（下个月再开工，慢牛环境下不紧急）

- [ ] **P0** 跑一次 dual_engine bug-fix 后对照回测（确认修复后仍劣于 v4_pure 单跑，给 dual_engine 钉死刑棺材板）
- [ ] **P1** `risk.json` 新增 `regime_overrides.bear`（max_daily_picks=2 / min_v4_total_score=50 / max_position_total=0.4），同步应用到 `backtest_strategy.py` 与 `searchv1.py`，跑回测验证 bear 段累积 log 从 -0.115 改善到接近 0
- [ ] **P2** risk-off 自动开关：连续 N 期累计亏 ≥X% → 强制空仓 M 期
- [ ] **P3** 把 P1 的 ATR 动态止损从 backtest 搬到 searchv1，让实盘也用 ATR-based 止损价
- [ ] **P4** hybrid 代码标记 `[DEPRECATED]`，从 `strategy.json` 暴露选项中移除（防止下次会话默认切回）
- [ ] **P5** 在 `config/strategy.json` 加注释字段写明 hybrid 为何被弃用，附本表数据指针

### 关键回测产物（追溯证据）

- `output/backtest_compare/B_v4_pure_systematic.log` — bug 前 v4_pure 单跑
- `output/backtest_compare/C_hybrid_systematic.log` — bug 前 hybrid 单跑（灾难）
- `output/backtest_compare/A_dual_engine_systematic.log` — bug 前 dual_engine（-11.79%）
- `output/backtest_compare/G_v0.6_fix_3y.log` — **bug 修复后 v4_pure（当前 baseline，+12.52%）**
- `output/backtest_compare/I_hybrid_bugfix_3y.log` — **bug 修复后 hybrid（仍灾难，-19.35%）**
- `output/backtest/backtest_detail_3y_20260427_223517.csv` — v4_pure 修复版每期明细
- `output/backtest/backtest_detail_3y_20260427_234947.csv` — hybrid 修复版每期明细

### 不要再做的事（避坑清单）

- ❌ 不要把 `strategy.json` 改回 `dual_engine` 或 `hybrid`，除非完成 P1 + P2 后重新对比
- ❌ 不要相信"bull 市该上 hybrid 追龙头"的直觉——回测里 hybrid 在 bull 市单期均值 -0.82%，是被龙头追到顶反向止损的反复挨打
- ❌ 不要在 v4_pure 选股稀疏（如今天只 1 只）时硬补——稀疏是 v4_pure 的 feature，强行补的都是低质量信号
- ❌ 不要混用 hybrid 和 v4_pure 的 v4_overrides 配置——`v4_overrides` 仅在 active_mode==v4_pure 时生效，hybrid 不读

