---
tags: [sources]
created: 2026-09-22
updated: 2026-09-22
sources: ["raw/vnpy_ctp/vnpy_ctp/gateway/ctp_gateway.py"]
---

# vnpy_ctp 源码摘要

来源：`raw/vnpy_ctp/vnpy_ctp/gateway/ctp_gateway.py`、`api/vnctp/vnctpmd/vnctpmd.cpp`、`test/test_md.py`。

底层 C++ 扩展暴露 `MdApi` 与 `TdApi`；网关类再包一层 VeighNa 数据对象转换。行情和交易是独立连接。交易连接后需要终端认证、登录；正式下单还需要结算确认等状态。

本次安装 6.7.11.4，`createFtdcMdApi(path, production_mode)` 和 `createFtdcTraderApi(path, production_mode)` 都需要两个参数。课程参数保留 `True`，这是 CTP API 环境开关；本项目仍连接 SimNow 仿真地址，不等同于开启真实交易。`subscribeMarketData` 一次接收一个字符串。

`CtpMdApi.onRtnDepthMarketData` 要查合约映射，合约信息尚未就绪时会过滤行情。最小作业直接继承 MdApi，因此能展示最底层数据，不依赖 MainEngine 或合约查询。

继续读：[ctp-lifecycle](../concepts/ctp-lifecycle.md)、[ctp-demo](../code/ctp-demo.md)。
