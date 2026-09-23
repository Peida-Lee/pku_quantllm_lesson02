---
tags: [concepts]
created: 2026-09-22
updated: 2026-09-22
---

# 共享策略代码不等于共享成交条件

VeighNa 可让同一策略模板在回测引擎和实盘引擎下运行。复用的主要是信号逻辑与统一回调接口，成交条件必须另外检验。

历史 K 线缺少完整盘口队列；触及某价格不一定能成交，涨跌停和停牌会限制交易。实盘还有延迟、部分成交、撤单、拒单、重连和账户对账。仅看历史最高最低价撮合会忽略这些约束。

因子 RankIC 衡量预测排序；十分组未来收益均值也不是已经实现的净值。重叠收益不能直接每日复利。应在选定信号后，再做不重叠持仓、成本和成交限制的策略回测。

关联：[cta-engine](../entities/cta-engine.md)、[factor-validation](factor-validation.md)、[order-plan](../code/order-plan.md)。
