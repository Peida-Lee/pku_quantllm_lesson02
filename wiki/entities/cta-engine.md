---
tags: [entities]
created: 2026-09-22
updated: 2026-09-22
---

# 单标的策略模板与策略引擎

来源：`raw/vnpy_ctastrategy/vnpy_ctastrategy/template.py#CtaTemplate`、`engine.py#CtaEngine`。

策略模板负责“何时产生何种交易意图”；策略引擎负责生命周期、行情分发、订单归属、回报、停止单、参数和状态。引擎按 vt_symbol 找策略，又按 vt_orderid 找订单所属策略。

初始化完成并不等于允许交易。模板的 inited 和 trading 区分初始化与启动。buy=买入开多，sell=卖出平多，short=卖出开空，cover=买入平空。开平仓语义不同，不能仅用买卖方向替代。

关联：[vnpy-ctastrategy](../sources/vnpy-ctastrategy.md)、[strategy-types](../concepts/strategy-types.md)、[backtest-vs-live](../concepts/backtest-vs-live.md)。
