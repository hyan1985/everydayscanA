# everydayscanA - 多策略每日选股

## 在线看板

流水线跑完后自动发布到 GitHub Pages：

- 首页（自动跳转）：https://hyan1985.github.io/everydayscanA/
- 统一看板：https://hyan1985.github.io/everydayscanA/unified_dashboard.html
- 分支部署备用路径：https://hyan1985.github.io/everydayscanA/output/unified_dashboard.html

> **Pages 设置**：Settings → Pages → Source 选 **GitHub Actions**（推荐）。若仍用 Deploy from branch，首页也会跳转到看板。

### Actions 跑失败时排查

1. **Settings → Actions → General → Workflow permissions** 选 **Read and write permissions**（否则 bot 无法 push 回仓库）
2. **Secrets** 中确认已配置 `TUSHARE_TOKEN`
3. 到 [Actions](https://github.com/hyan1985/everydayscanA/actions) 查看日志；选股步骤成功但 push 失败时，看板仍会通过 Pages 发布
