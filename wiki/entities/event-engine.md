---
tags: [entities]
created: 2026-09-22
updated: 2026-09-22
---

# 事件总线 EventEngine

源码：`raw/vnpy/vnpy/event/engine.py#EventEngine`，`raw/vnpy/vnpy/trader/gateway.py#BaseGateway.on_tick`。

`put(event)` 将事件入队，工作线程 `_run` 取出，`_process` 按事件类型调用处理器；定时线程生成定时事件。注册函数同类型去重。一个行情事件可同时被缓存和策略引擎消费。

分发线程上的回调是顺序执行的，慢处理器会阻塞后续事件。该总线是进程内队列，不是持久化消息系统；崩溃后不能把它当可恢复的历史数据库。需额外异常隔离和持久化设计。

关联：[architecture](../concepts/architecture.md)、[oms-main-engine](oms-main-engine.md)、[cta-engine](cta-engine.md)。
