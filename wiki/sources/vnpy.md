---
tags: [sources]
created: 2026-09-22
updated: 2026-09-22
sources: ["raw/vnpy/vnpy/trader/engine.py"]
---

# vnpy 主框架源码摘要

来源：`raw/vnpy/vnpy/event/engine.py`、`raw/vnpy/vnpy/trader/{engine,gateway,object}.py`。具体 Git 提交见 `reports/source_commits.json`。本次聚焦以下调用链，不声称精读全部仓库。

`EventEngine` 使用队列和分发线程把事件交给注册函数；`BaseGateway` 把不同柜台数据转成公共对象并推入总线；`MainEngine` 注册网关和应用并按名称路由；`OmsEngine` 维护最近行情、订单、成交、仓位和账户缓存。

例如 `BaseGateway.on_tick()` 同时推送通用行情事件及合约专属事件。策略通常通过策略引擎接收这些标准事件，不需要在策略里写 CTP 字段映射。

继续读：[event-engine](../entities/event-engine.md)、[oms-main-engine](../entities/oms-main-engine.md)、[data-objects](../entities/data-objects.md)。
