---
tags: [sources]
created: 2026-09-22
updated: 2026-09-22
sources: ["raw/vnpy_ctastrategy/vnpy_ctastrategy/engine.py"]
---

# vnpy_ctastrategy 源码摘要

来源：`raw/vnpy_ctastrategy/vnpy_ctastrategy/template.py` 与 `engine.py`；多标的/价差扩展见 `raw/vnpy/docs/community/app/portfolio_strategy.md`、`spread_trading.md`。

`CtaTemplate` 定义 `on_init/on_start/on_stop/on_tick/on_bar/on_order/on_trade` 和 buy/sell/short/cover 等便利函数。`CtaEngine.process_tick_event` 按合约定位策略；引擎管理策略初始化、运行状态和订单归属。

`CtaTemplate.send_order` 先检查 trading 状态；`CtaEngine.send_order` 查合约并按最小价位/最小数量调整，再进入限价委托或停止单流程。`process_trade_event` 去重后更新策略仓位，再通知策略。

继续读：[cta-engine](../entities/cta-engine.md)、[strategy-types](../concepts/strategy-types.md)、[backtest-vs-live](../concepts/backtest-vs-live.md)。
