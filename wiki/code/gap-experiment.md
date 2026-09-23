---
tags: [code]
created: 2026-09-22
updated: 2026-09-22
---

# 本次跳空动量因子实验

程序：`projects/gap_factors/run.py`；事前方案：`projects/gap_factors/protocol.md`；正式结果：`reports/gap_round_01_v2/report.md`。

本轮实际跑了 16 个事先固定的候选，使用 819 只股票、2,668,723 行原始历史数据；研究区间内经历史成分和当日成交量过滤后 895,685 行，再按标签及因子有效性分段。

训练规则仅入选 g16_gap_intraday（隔夜与日内收益差的五日均值），训练/验证/历史测试平均 RankIC 分别为 0.04195、0.03119、0.01585。验证期通过预定多重比较标准。它混合了隔夜相对强度与日内反转，不能据此宣布“纯开盘跳空动量”普遍成立。

首次运行因教材 ts_mean 的逐行 Python 调用太慢而中止，保存在 `reports/gap_round_01`。第二次使用等价原生滚动均值/总体标准差，先通过含缺失值的等价测试再重跑，没有修改候选经济含义或验证门槛；g09 的 0/1 比较值显式转为浮点，避免原实现整数均值截断。

重要修正：教材 `prepare_data(filters)` 把成分过滤应用到 raw_df，但评估读取 result_df，绕开过滤；本次显式连接历史成分键。标签以市场交易日历连接，处理停牌/缺失端点，并剔除跨段标签。不报告不成立的“年化策略收益”。

关联：[factor-validation](../concepts/factor-validation.md)、[backtest-vs-live](../concepts/backtest-vs-live.md)。
