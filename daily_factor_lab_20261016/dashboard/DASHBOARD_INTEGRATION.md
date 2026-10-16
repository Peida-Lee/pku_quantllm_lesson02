# 本地回测仪表盘

仪表盘使用 Data 共享运行时。受保护的顶栏、图表编辑、数据源检查、复制与布局保存保留。应用标识为 `dashboard:fb12cd53-dd23-4222-85f4-3fca6dfeca79`，刷新结果时须保留。

## 数据与接口

`src/data.json` 顶层保留 `id`、`surface: dashboard`、`title`、`generatedAt`、`buildStatus`，以及当前运行的 `run_id`、`config`、`files`、`strategies`、`diagnostics`。`queries` 使用固定四个键：

- `summary.rows`：`factor, name, scenario, total_return, annual_return, sharpe, max_drawdown`。
- `curves.rows`：`date, factor, name, scenario, nav, nav_index, drawdown`。`nav_index=nav/capital`，日期使用实际 ISO 日期，保留全量交易日。
- `yearly.rows`：`factor, name, scenario, year, return`。年度图仅取四位数年份，时间留出区间可留在源数据。
- `ic.rows`：`factor, name, mean_rank_ic, ci95_low, ci95_high, p_raw_hac_normal, days`。

每个 query 保留 `source` 和 `methods`，来源元数据须与当前 run 对齐。场景为 `base/gross/stress`。因子为 `momentum60/reversal5/lowvol20/liquidity20/combo`；可选参考为 `reference`。默认图与指标用 `base`。

`GET /api/status` 返回 `{busy, progress, error, latest_run}`，`latest_run` 为运行 ID。运行中约每 2.5 秒轮询，闲置时约每 12 秒轮询。

`POST /api/run` 接收 JSON Config：表单可修改 `start/end/capital/top_n/rebalance/commission/slippage/min_avg_amount/open_guard`，其余使用 snapshot.config 或后端默认值。

`GET /api/result` 返回当前完整结果。页面图表以已构建 snapshot 为唯一证据；新运行结果生成并重新构建后，页面显示“加载已更新页面”，用户点击刷新以同时加载新证据和来源元数据。

`files` 是 `{report: 'run_id/report.md', ...}`；`strategies` 是 `{factor: {daily: 'run_id/base/factor/daily.csv', trades: ..., holdings: ...}}`。页面也兼容 `files.strategies`。所有下载统一使用 `GET /api/download?path=<URL编码的outputs相对路径>`。

## 构建和服务

使用共享 preparer 创建项目；普通构建无需 npm 安装。构建命令：

```powershell
& $env:FACTOR_NODE (Join-Path $env:FACTOR_DATA_PLUGIN 'scripts/data-app.mjs') build --project-dir './dashboard' --separate-data
```

从项目根目录执行。`FACTOR_DATA_PLUGIN` 指向已安装 Data 插件目录，`FACTOR_NODE` 指向 Node 可执行文件；后端会尝试自动发现，也可显式设置这两个环境变量。公开副本不包含受保护运行时源码，首次构建可运行 `python bootstrap_dashboard.py`，以官方准备器生成本地运行时并复制可编辑内容。

Python 后端在 `127.0.0.1:8776` 同源提供完整 `dist` 文件夹（包括内容寻址的 JSON sidecar 与 manifest）及上述 API。不要只复制 `index.html`，不要重复启动服务。

## 口径

本地历史研究模拟，不连接券商。固定四个因子方向与等权组合，全部显示，不按本次结果择优。费用比较使用同一交易逻辑。显示 HAC 原始 P 与 95% 区间；动态股票池参考明确为无费用、非指数、非可交易绩效。完整执行假设和数据限制通过当前运行 report 下载和源检查提供。
