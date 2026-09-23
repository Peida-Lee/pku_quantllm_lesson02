---
tags: [concepts]
created: 2026-09-22
updated: 2026-09-22
---

# 数据结构与业务流程标准化

数据标准化把 CTP 的 `InstrumentID/LastPrice/OrderStatus` 转成统一的 `symbol/last_price/Status`。合约标识 `vt_symbol` 含交易所，如 `rb2701.SHFE`；订单还带网关、会话相关标识，不能把同账号所有订单误认为自己的订单。

业务标准化把异步柜台步骤藏在网关中：连接→认证→登录→结算确认→查询合约/账户/持仓；订阅和交易请求使用统一接口，结果仍通过事件返回。标准化是统一语义，不会把异步交易变成同步成交。

来源：[vnpy-ctp](../sources/vnpy-ctp.md)。对象清单：[data-objects](../entities/data-objects.md)；连接时序：[ctp-lifecycle](ctp-lifecycle.md)。
