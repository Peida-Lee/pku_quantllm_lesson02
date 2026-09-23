---
tags: [code]
created: 2026-09-22
updated: 2026-09-22
---

# 价格突破后下单：供确认的扩展方案

Issue 明确要求：“因为这一步我不是那么确定，你先给出方案，等我阅读确认后再执行。”本次完成以下方案，未启用交易下单。教学包中的 breakout 示例只作为只读参考。

1. 仿真交易通道认证→登录→结算确认→查询目标合约、账户和持仓；全部就绪才允许触发。
2. 指定一个合约、一个固定价格阈值，最多开多 1 手。仅当有效新行情从阈值下方穿越到上方时触发，第一条行情已在阈值上方不自动补单。
3. 检查报价新鲜度、最小变动价位、涨跌停、可用资金和活动委托。以符合价位约束的限价发单，不默认使用市价。
4. `READY → SUBMITTING → WORKING/PARTIAL → FILLED/CANCELLED/REJECTED`。每次运行最多一次交易意图；断线重连不自动重复开仓，先与柜台对账。
5. 回报按本程序 FrontID、SessionID、OrderRef 及返回的交易所订单号识别；成交按 TradeID 等唯一标识去重。共享教学账号的其他订单不归本程序处理。
6. 请求本地返回成功、委托受理、实际成交分开记录。单独覆盖拒单、部分成交、撤单和断线测试；风控阈值和撤单超时在实现前写入配置。

待确定参数：仿真合约、触发价、限价规则、超时撤单秒数、是否允许次日继续。确认后才实现接单闭环；当前 `demo.py` 是只读行情程序。

来源：[vnpy-ctp](../sources/vnpy-ctp.md)、课程 `projects/ctp_breakout_demo/README.md`；关联 [data-objects](../entities/data-objects.md)、[backtest-vs-live](../concepts/backtest-vs-live.md)。
