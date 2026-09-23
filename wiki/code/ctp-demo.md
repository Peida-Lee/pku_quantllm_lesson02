---
tags: [code]
created: 2026-09-22
updated: 2026-09-23
---

# 本次底层行情程序与验证记录

程序：`projects/ctp_demo/demo.py`。双接口登录、逐条订阅、行情打印、10 秒心跳、断线重连恢复、Ctrl+C 释放和独立运行日志均已实现。没有委托/撤单/结算确认功能。

默认运行直到 Ctrl+C；`--duration 45` 是此次验证的限时选项，`--offline` 明确输出合成行情。每次输出目录独立，拒绝覆盖。配置从课程示例提取到 git 忽略的 `config.simnow.local.json`，日志不输出账号密码。

5 个回调测试通过，合成回放收到 3 条 tick 并完成一次重连重新订阅。9 月 22 日的 `reports/ctp_live_01` 是未连接成功的历史尝试。9 月 23 日的 `reports/ctp_live_20260923_retry01` 已完成真正的 SimNow 联调：行情/交易登录成功，4 个订阅成功，60 秒收到 202 条回调，未下单。

本次 au2612、ag2612、cu2610 分别收到 82、93、26 条持续更新行情；rb2701 仅 1 条上一时段快照。已观测到三个持续更新合约的成交量增长，但未进行全市场排名，不能把它们断言为“当前第一主力”。

关联：[ctp-lifecycle](../concepts/ctp-lifecycle.md)、[order-plan](order-plan.md)。
