---
tags: [concepts]
created: 2026-09-22
updated: 2026-09-22
---

# 底层 CTP 的异步连接与行情生命周期

行情路径：创建 MdApi → 注册前置 → init → onFrontConnected → reqUserLogin → onRspUserLogin → subscribeMarketData → onRspSubMarketData → onRtnDepthMarketData。

交易登录路径：创建 TdApi → 注册前置 → init → onFrontConnected → reqAuthenticate → onRspAuthenticate → reqUserLogin → onRspUserLogin。本作业到登录为止；进一步下单方案见 [order-plan](../code/order-plan.md)。

请求返回 0 仅表示本地接受发送；必须继续看服务器回调。订阅回报成功也不能证明该合约活跃或已有行情。断线时清理登录状态；CTP 自行重连，onFrontConnected 后重新登录订阅；不要反复 create/init。

主线程保持存活，Ctrl+C 在主线程释放两个 API。回调线程中不做长耗时因子计算，也不在自己的回调线程调用 exit。行情保留 TradingDay 与 ActionDay，夜盘交易日不能直接当自然日期。

来源：[vnpy-ctp](../sources/vnpy-ctp.md)；实现与实测：[ctp-demo](../code/ctp-demo.md)。
