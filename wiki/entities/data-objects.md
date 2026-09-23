---
tags: [entities]
created: 2026-09-22
updated: 2026-09-22
---

# 统一交易数据对象

来源：`raw/vnpy/vnpy/trader/object.py` 与 `constant.py`。

|对象|用途|容易混淆的点|
|---|---|---|
|TickData|最新价、盘口、交易所与时间|不是一次撮合成交的逐笔成交记录|
|BarData|一个周期的开高低收及量|周期结束前不应当作完整收盘信息|
|OrderRequest|请求方向、开平、数量、价格|请求不等于已成交|
|OrderData|委托状态与已成交数量|可能部分成交、拒单或取消|
|TradeData|成交回报|用唯一标识去重后更新仓位|
|PositionData|账户实际持仓|不能只从发出的订单推断|
|ContractData|合约最小价位、乘数、数量限制|限价和手数必须匹配规则|

关联：[standardization](../concepts/standardization.md)、[oms-main-engine](oms-main-engine.md)。
