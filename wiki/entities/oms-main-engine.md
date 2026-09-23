---
tags: [entities]
created: 2026-09-22
updated: 2026-09-22
---

# 主引擎路由与订单管理缓存

源码：`raw/vnpy/vnpy/trader/engine.py#MainEngine`、`#OmsEngine`。

MainEngine 装配网关与应用；`send_order(req, gateway_name)` 按网关名称转发。它是统一入口，不负责凭空判断委托已经成交。

OmsEngine 的字典保存 ticks/orders/trades/positions/accounts/contracts，并单独维护 active_orders。行情以 vt_symbol 更新，订单以 vt_orderid 更新，非活动订单从 active_orders 移出。它保存最近状态与本进程接收的回报，不是完整历史行情数据库，也不能保证启动时已与柜台对账。

关联：[data-objects](data-objects.md)、[event-engine](event-engine.md)、[cta-engine](cta-engine.md)。
