# everydayscanA

四策略 A 股每日选股流水线：自动跑策略、聚合结果、生成看板，并支持 GitHub Actions 定时执行 + Pages 在线浏览。

## 在线看板

流水线跑完后自动发布到 GitHub Pages：

| 页面 | 链接 |
|------|------|
| 首页（自动跳转） | https://hyan1985.github.io/everydayscanA/ |
| 统一看板 | https://hyan1985.github.io/everydayscanA/unified_dashboard.html |

## 策略说明

| 策略 | 目录 | 说明 |
|------|------|------|
| 擒龙猎手 | `strategies/ql_dragon` | 造龙结构多因子扫描 |
| 主升行情启动 | `strategies/zhush_mainrise` | 十五五主升浪 + 资金共振 |
| 盘后扫描追随 | `strategies/ph_afterclose` | 龙头跟随 / 板块队形 |
| 存储 IPO 供应链 | `strategies/lh_quant` | 长鑫 / 长存供应链映射扫描 |

聚合器会合并四路结果，标注**多策略共振**标的，并输出 HTML 看板、微信文本、汇总 CSV。

> `strategies/lh_quant` 里另有「量化蓄势突破」脚本，已从每日流水线移除（耗时长），需要时可单独手动运行。

## 项目结构

```
.
├── run_all.sh          # 主入口：预拉取 → 四策略 → 聚合
├── aggregate.py        # 统一聚合 + 看板渲染
├── config.yaml         # 策略字段映射
├── config/concepts.yaml
├── quant_data/         # Tushare 缓存层（Parquet）
├── strategies/         # 各策略代码
├── output/             # 统一输出（看板 / CSV / 微信文本）
└── .github/workflows/  # GitHub Actions
```

## 本地运行

### 1. 环境

- Python 3.11+
- [Tushare](https://tushare.pro) Token

```bash
pip install -r requirements.txt
export TUSHARE_TOKEN="你的token"
```

macOS 也可把 Token 存钥匙串（服务名 `cursor-quant-tushare`），`run_all.sh` 会自动读取。

### 2. 一键跑全流程

```bash
bash run_all.sh
```

输出：

- `output/unified_dashboard.html` — 统一看板
- `output/unified_wechat.txt` — 微信分享文本
- `output/unified_all_picks.csv` — 汇总 CSV

本地打开看板：

```bash
open output/unified_dashboard.html
```

### 3. 清除 Parquet 缓存

```bash
bash run_all.sh --clear-cache
```

`data/` 目录体积会很大（本地缓存，不提交 Git）。若 Cursor 卡顿，建议把 `data/` 移到项目外或依赖 `.cursorignore`。

## GitHub Actions 自动化

工作流：`.github/workflows/daily-run.yml`

- **定时**：工作日北京时间 **20:00** 自动执行
- **手动**：Actions →「每日选股流水线」→ Run workflow
- **产物**：更新 `output/` 并部署 GitHub Pages

### 首次配置

1. **Secret**：Settings → Secrets → `TUSHARE_TOKEN`
2. **权限**：Settings → Actions → General → Workflow permissions → **Read and write permissions**
3. **Pages**：Settings → Pages → Source → **GitHub Actions**

### CI 超时（可选环境变量）

GitHub 无本地缓存，默认超时比本机更长：

| 变量 | CI 默认 | 本机默认 |
|------|---------|----------|
| `TIMEOUT_QL_DRAGON` | 360s | 180s |
| `TIMEOUT_ZHUSH` | 360s | 150s |
| `TIMEOUT_PH_AFTERCLOSE` | 360s | 180s |
| `TIMEOUT_STORAGE_IPO` | 240s | 120s |

Actions 会缓存 `data/` Parquet，第二次起明显更快。

### 跑失败时排查

1. Actions 日志里看哪个策略失败 / 超时
2. 确认 `TUSHARE_TOKEN` 有效且有足够积分
3. push 失败不影响 Pages 发布，但需检查 Workflow permissions

## 配置

- **策略字段**：编辑 `config.yaml`
- **概念 / 题材**：编辑 `config/concepts.yaml`（会自动同步到各策略）

## 免责声明

本项目仅为量化模型推演与数据展示，**不构成任何投资建议**。股市有风险，投资需谨慎。
