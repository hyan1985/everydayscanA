# CHANGELOG

用于记录量化选股项目中的策略、数据口径与执行规则变更，确保可追溯、可复盘、可回滚。

---

## 实际变更记录

### [v0.5.0] - 2026-04-27 — 语义重定义：v4_pure → 「盘整启动专用」

#### 1) 变更摘要

- 变更类型：`策略参数` / `因子权重` / `执行规则`
- 负责人：hyan
- 关联讨论：意识到「主升浪龙头」要求过高、不可复制，改为更稳健的「盘整结束、刚启动右侧确认前夜」定义。

#### 2) 重要决策

- **保留 dual_engine 自动切换框架**：`bull→hybrid(主升浪追龙头)`，`range/bear→v4_pure(盘整启动专用)`，无需手工切模式。
- **v4_pure 引擎语义重定义**：从"盘整 + 蓄势 + 板块 + 估值 + 趋势"加权，重定义为"以 imminent 为核心 0.45、辅以蓄势 0.20、板块只要求不死、趋势权重削弱到 0.20"。
- **入场信号优先级按 mode 切换**：
  - `hybrid`：`cross_flow > breakout > setup` (右侧动能优先，吃肉)
  - `v4_pure`：`setup > cross_flow > breakout` (埋伏前夜优先，吃饭)
- **执行参数按 mode 切换**：通过 `risk.json::v4_overrides` 与 `trade.json::v4_overrides` 在 v4 激活时自动覆盖严格阈值，不破坏 hybrid 模式默认值。

#### 3) 具体变更

- `config/risk.json`
  - 新增 `v4_overrides`：`max_runup_10d_pct=8.0`、`max_near_high_20d_ratio=0.92`、`min_accumulation_score=55`、`box_low_position_min=0.25`、`box_low_position_max=0.65`。
  - 默认（hybrid 用）保持上一版宽松：runup≤12、box [0.35,0.75]。
- `config/trade.json`
  - 新增 `v4_overrides`：`hard_stop_loss_pct=0.05`、`tp1_pct=0.06`、`tp2_pct=0.12`、`max_holding_days=20`、`floating_*` 同步收紧。
  - 含义：v4 走"波段思路"，不奢望主升浪。
- `config/cross_flow.json`
  - `range_min_rotation_score` 45 → 30；`bear_min_rotation_score` 55 → 35。
  - 含义：盘整启动模式下不要求龙头板块，板块只要不死即可。
- `searchv1.py::run_daily_selection`
  - v4 总分权重 `imminent*0.30 + accum*0.20 + rot*0.10 + val*0.05 + ranked*0.35` 改为 `imminent*0.45 + accum*0.20 + rot*0.10 + val*0.05 + ranked*0.20`。
  - `entry_priority` 按 mode 切换（v4: setup=3, cross=2, breakout=1）。
  - 加 `apply_mode_overrides()` 工具函数，自动把 `v4_overrides` 合并到 effective `risk_cfg`/`trade_cfg`。
  - 日志新增「mode_label」与「生效阈值」一行（runup/near_high/box/止损止盈/持仓上限）。
- `searchv1.py::load_risk_config / load_trade_config`
  - 之前用 DEFAULT 字典做白名单，会丢掉 dict 类型的扩展字段（`v4_overrides`、`context_leaders_by_concept` 等）。改为透传所有非默认字段，保证嵌套配置可用。
- `searchv1.py::_exec_bundle`
  - `max_hold_days` 从硬编码 30 改为读取 `trade_cfg.max_holding_days`（v4 模式自动 20）。

#### 4) 变更原因

- 触发背景：用户反思「主升浪 = 板块龙头」要求过高、不可重复，希望放宽到「盘整结束、准备启动上涨」更适合波段操作。结合 `RETAIL_SWING_PLAYBOOK.md` 的 +5%~+15% 目标区间，v4 应该更靠"吃饭"而不是"吃肉"。
- 目标：
  1. 提高候选池规模（每周市场可选标的更多）
  2. 提高胜率（45-55% 区间），可接受单笔盈亏比下降到 2-3x
  3. 缩短持有期，提高资金周转
  4. 牛市仍能切回 hybrid 追龙头，不放弃大行情

#### 5) 验证结果（回测或模拟）

- 验证区间：建议 2024-01 ~ 2026-04 横盘+牛市样本，分别对比新旧 v4
- 关键观察项：
  - 新 v4 候选数 vs 旧 v4 候选数（应增加 30%+）
  - Top10 中 `runup_10d_pct ≤ 8%` 占比（应 ≥ 80%）
  - Top10 中 box 位置分布（应集中在 0.25-0.65）
  - 5 日 / 10 日胜率（目标 ≥ 50%）
  - 平均持有天数（应从 ~25 降到 ~15）

#### 6) 风险评估

- 可能副作用：v4 候选池扩大后噪音增多，可能拉入"盘整中但永远不启动"的死水票；用 `imminent_score ≥ 60` + `mf_pos_days ≥ 1` 过滤。
- 失效场景：单边大牛市中 v4 表现劣于 hybrid（追不上龙头）；但此时 `regime=bull`，自动切回 hybrid。
- 监控指标：每日观察 `mode_label` 输出；连续 5 日处于 `v4_pure` 但板块平均涨幅 > 3% 时，考虑手工切回 hybrid。

#### 7) 上线与回滚

- 生效日期：2026-04-28（明日）。
- 生效范围：模拟盘 / 离线选股。
- 回滚条件：连续 10 个交易日 5 日胜率 < 40%；或回滚到 v0.4.0。
- 回滚版本：v0.4.0。

#### 8) 复盘结论（后补）

- 实际表现与预期偏差：待补充
- 后续动作：待补充

---

### [v0.4.0] - 2026-04-27 — 「盘整结束、即将启动主升浪」专项重构

#### 1) 变更摘要

- 变更类型：`因子权重` / `风控规则` / `执行规则` / `数据口径`
- 负责人：hyan
- 关联文档：本次会话诊断报告（与 `RETAIL_SWING_PLAYBOOK.md` 对齐）

#### 2) 具体变更

- 板块过滤：`searchv1.py`
  - 变更前：用 20 日动量板块均值取前 35% 做硬过滤（`concept_strength_keep_ratio=0.35`），把"低位待发"板块直接拦掉。
  - 变更后：板块动量均值仅作为参考变量（重命名 `concept_mom20_strength`），改用 `concept_rotation_score` 做软加权过滤（阈值 `{regime}_min_rotation_score - 5`）；`concept_strength_keep_ratio` 提到 `0.6` 仅作弱降权。
- 箱体位置：`config/risk.json` + `searchv1.py`
  - 变更前：`box_low_position_max=0.4`（仅过滤低位）。
  - 变更后：`box_low_position_min=0.35` / `box_low_position_max=0.75`，区间过滤；同步 `pre_cross_box_position_max` 由 0.45 提到 0.55。
- 蓄势分（accumulation）：`searchv1.py`
  - 变更前：`obv*0.6 + box_low*0.4`，OBV 主导，盘整期得分偏低。
  - 变更后：`obv*0.35 + box_low*0.35 + 量能收缩*0.30` 三因子合成；`min_accumulation_score` 由 65 降到 58。
- 新增因子：`searchv1.py::FactorCalculator`
  - `volume_contraction_score`（5 日 / 20 日均量在 [0.7,1.0] 给满分，三角衰减）
  - `amplitude_contraction_pct`（5 日均振幅在 60 日内的分位）
  - `ma_cluster_tightness`（MA5/10/20 最大相对距离 → 粘合度）
  - 组合因子 `breakout_imminent_score`（量能 0.20 + 振幅 0.15 + 箱体 0.15 + 均线 0.20 + W&R 0.15 + 资金 0.15）
- v4_pure 引擎重定义：`searchv1.py`
  - 变更前：`accumulation*0.45 + box*0.25 + rotation*0.20 + valuation*0.10`，完全丢掉趋势打分。
  - 变更后：`breakout_imminent*0.30 + accumulation*0.20 + rotation*0.10 + valuation*0.05 + ranked_total*0.35`，保留 35% 横截面动能信号。
- 追高过滤豁免：`searchv1.py`
  - 变更前：`macd_hist_slope >= 0` 一刀切，刚启动 1-2 天的票被误杀。
  - 变更后：严格档放宽到 `>= macd_hist_slope_min` 且 `is_pre_cross` 命中时豁免；mild 档进一步放宽到 `>= -0.005`；`config/cross_flow.json::macd_hist_slope_min=-0.002`。
- `is_pre_cross` 收紧：`searchv1.py::cross_signal_features`
  - 变更前：`shrink_ratio < 1.0`（任何微缩都算）。
  - 变更后：`shrink_ratio < is_pre_cross_shrink_ratio_max`（默认 0.9，可配）。
- 行业 z-score 死代码清理：`searchv1.py::StockScoringEngine.rank`
  - 变更前：计算 `pb_z`/`pe_z` 但未使用；`runup_penalty` 仅 5% 权重不够拉差。
  - 变更后：删除 `pb_z`/`pe_z`；`runup_penalty` 权重 5% → 15%。
- top_inst.net_buy 单位探针：`searchv1.py`
  - 变更前：硬编码假设单位为「万元」，与 Tushare 实际可能为「元」不一致。
  - 变更后：用绝对值中位数自动识别（>= 1e6 视为「元」并 `/1e4`），日志输出推断结果。
- 主题匹配：`config/themes.json` + `searchv1.py`
  - 变更前：alias_map 写死在代码、`fuzzy_cutoff=0.35`、`top_n=3`，易引噪。
  - 变更后：`themes.json` 加 `aliases`/`fuzzy_cutoff`/`fuzzy_top_n` 字段，`fuzzy_cutoff` 默认 `0.55`、`top_n=2`；新增 `load_theme_config()` 统一注入。
- 日志增强：`searchv1.py`
  - 新增「strict / mild / fallback 三档计数」、「top_inst 单位推断」、「主题匹配 fuzzy 配置」三行输出，便于审查。
- 看板增强：`build_dashboard.py`
  - 新增「多维分数对比」分组柱状图（突破前夜分 / 吸筹分 / 板块轮动分 / 总分）。
  - 新增「关键时序」K 线 + MA5/10/20 + 量能图（数据源 `backtest_cache/daily/`）。

#### 3) 变更原因

- 触发背景：原扫描器与"盘整结束、即将启动主升浪"意图存在多处冲突——板块硬过滤会把低位待发板块剔除；`box_low_position_max=0.4` 偏左侧抄底；`v4_pure` 模式丢失趋势分；`macd_hist_slope>=0` 误杀刚启动票。
- 目标：让选股结果更贴近"突破前夜"形态（缩量盘整 + 振幅收敛 + 均线粘合 + 资金抬头），降低追高与左侧抄底的样本污染。

#### 4) 验证结果（回测或模拟）

- 验证区间：待跑（建议至少 2024-01 ~ 2026-04 横盘+牛市样本）
- 关键观察项：
  - Top10 候选中 `breakout_imminent_score >= 60` 的占比
  - Top10 中 box 位置分布（应以 0.35–0.75 为主）
  - Top10 中 `runup_10d_pct <= 12` 占比（应 ≥ 80%）
  - 5 日 / 10 日胜率与原版本对比

#### 5) 风险评估

- 可能副作用：v4 加入 35% 趋势分后，弱市可能跟入"已启动+追高"票；用 `runup_penalty` 0.15 权重 + `max_runup_10d_pct=12` 抑制。
- 失效场景：
  - 板块软加权后，若板块整体哑火，组合表现会跟随板块衰减；
  - 量能收缩因子在「连续涨停 → 缩量」假阳性场景需手工剔除。
- 监控指标：每日检查日志中 `branch_used`、`top_inst_unit_hint`、`strict/mild/fallback` 三档计数；连续 5 日 `fallback` 触发说明阈值过严。

#### 6) 上线与回滚

- 生效日期：2026-04-27 起的下一交易日。
- 生效范围：模拟盘 / 离线选股。
- 回滚条件：
  - 连续 10 个交易日 5 日胜率比 v0.3.x 显著劣化（≥ 5pp）；或
  - 触发 `fallback` 分支占比 > 30%。
- 回滚版本：v0.3.x（保留 `searchv1.py` git 历史）。

#### 7) 复盘结论（后补）

- 实际表现与预期偏差：待补充
- 后续动作：待补充

---

## 策略参数变更记录模板

### [版本号] - YYYY-MM-DD

#### 1) 变更摘要

- 变更类型：`策略参数` / `因子权重` / `风控规则` / `数据口径` / `执行规则`
- 负责人：
- 关联文档（回测报告/任务链接）：

#### 2) 具体变更

- 变更项 1：
  - 变更前：
  - 变更后：
- 变更项 2：
  - 变更前：
  - 变更后：

#### 3) 变更原因

- 触发背景（市场变化/失效信号/风险事件）：
- 目标（提高收益/降低回撤/降低换手/提升稳健性）：

#### 4) 验证结果（回测或模拟）

- 验证区间：
- 样本内结果：
  - 年化收益：
  - 最大回撤：
  - 夏普：
  - 胜率：
  - 换手率：
- 样本外结果：
  - 年化收益：
  - 最大回撤：
  - 夏普：
  - 胜率：
  - 换手率：

#### 5) 风险评估

- 可能副作用：
- 失效场景：
- 监控指标与预警阈值：

#### 6) 上线与回滚

- 生效日期：
- 生效范围（模拟盘/实盘）：
- 回滚条件：
- 回滚版本：

#### 7) 复盘结论（后补）

- 实际表现与预期偏差：
- 后续动作：

---

## 记录示例

### [v0.1.0] - 2026-04-22

#### 1) 变更摘要

- 变更类型：`策略参数`
- 负责人：hyan
- 关联文档：`templates/策略回测报告模板.md`

#### 2) 具体变更

- 变更项 1：动量因子权重
  - 变更前：0.25
  - 变更后：0.35
- 变更项 2：单票权重上限
  - 变更前：10%
  - 变更后：8%

#### 3) 变更原因

- 触发背景：震荡市中回撤偏大，短期趋势因子解释力提升。
- 目标：控制回撤并提升风险调整后收益。

#### 4) 验证结果（回测或模拟）

- 验证区间：2023-01-01 ~ 2026-03-31
- 样本内结果：
  - 年化收益：18.2%
  - 最大回撤：-12.6%
  - 夏普：1.21
  - 胜率：56.4%
  - 换手率：0.42
- 样本外结果：
  - 年化收益：14.7%
  - 最大回撤：-9.8%
  - 夏普：1.08
  - 胜率：54.1%
  - 换手率：0.39

#### 5) 风险评估

- 可能副作用：趋势反转阶段可能增大回撤。
- 失效场景：突发事件驱动的高波动行情。
- 监控指标与预警阈值：7 日回撤 > 4% 触发审查。

#### 6) 上线与回滚

- 生效日期：2026-04-23
- 生效范围：模拟盘
- 回滚条件：连续 10 个交易日收益显著劣于基线策略。
- 回滚版本：v0.0.9

#### 7) 复盘结论（后补）

- 实际表现与预期偏差：待补充
- 后续动作：待补充
