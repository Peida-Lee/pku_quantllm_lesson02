---
tags: [concepts]
created: 2026-09-22
updated: 2026-09-22
---

# 单标的、多标的和价差策略的区别

|策略类型|典型问题|需要维护的状态|
|---|---|---|
|单标的时序策略|一份螺纹钢合约是否突破过去高点|该合约历史、仓位、活动委托|
|多标的投组策略|在股票池内排名后配置权重|同步截面、总风险预算、目标持仓、再平衡|
|价差套利类策略|两腿价差偏离是否值得交易|腿比例、价差合成、腿成交状态、裸露风险|

`vnpy_ctastrategy` 主要解决第一类；投组通常用 `vnpy_portfoliostrategy`，价差通常用 `vnpy_spreadtrading`。后两者本次阅读主框架内文档，没有额外声称已安装并联网验证。不要用两个互不知情的单合约策略替代一个需要同步风险控制的价差策略。

来源：[vnpy-ctastrategy](../sources/vnpy-ctastrategy.md)。关系：[architecture](architecture.md)、[gap-experiment](../code/gap-experiment.md)。
