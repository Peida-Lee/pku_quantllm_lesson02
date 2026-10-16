# 预制积木模块清单

由真实模块目录生成，共79项；自定义导入包会另外加入界面目录。

| 分类 | 模块 | 操作ID | 输入 | 输出 | 默认参数 |
|---|---|---|---|---|---|
| 行情数据 | 复权开盘价 | `open` | 无 | series | 无 |
| 行情数据 | 复权最高价 | `high` | 无 | series | 无 |
| 行情数据 | 复权最低价 | `low` | 无 | series | 无 |
| 行情数据 | 复权收盘价 | `close` | 无 | series | 无 |
| 行情数据 | 原始开盘价 | `raw_open` | 无 | series | 无 |
| 行情数据 | 原始最高价 | `raw_high` | 无 | series | 无 |
| 行情数据 | 原始最低价 | `raw_low` | 无 | series | 无 |
| 行情数据 | 原始收盘价 | `raw_close` | 无 | series | 无 |
| 行情数据 | 成交金额 | `amount` | 无 | series | 无 |
| 行情数据 | 原始累计复权因子 | `adjustment_factor` | 无 | series | 无 |
| 行情数据 | 成交笔数 | `trade_count` | 无 | series | 无 |
| 行情数据 | 已有收盘观测天数 | `history` | 无 | series | 无 |
| 行情数据 | 复权日收益率 | `daily_return` | 无 | series | 无 |
| 行情数据 | 归一化复权倍数 | `adjustment_scale` | 无 | series | 无 |
| 数值运算 | 数值常量 | `constant` | 无 | series | value=1.0 |
| 数值运算 | 相加 | `add` | x:series, y:series | series | 无 |
| 数值运算 | 相减 | `subtract` | x:series, y:series | series | 无 |
| 数值运算 | 相乘 | `multiply` | x:series, y:series | series | 无 |
| 数值运算 | 相除 | `divide` | x:series, y:series | series | 无 |
| 数值运算 | 乘方 | `power` | x:series, y:series | series | 无 |
| 数值运算 | 逐点较小值 | `minimum` | x:series, y:series | series | 无 |
| 数值运算 | 逐点较大值 | `maximum` | x:series, y:series | series | 无 |
| 数值运算 | 绝对值 | `abs` | x:series | series | 无 |
| 数值运算 | 取相反数 | `negate` | x:series | series | 无 |
| 数值运算 | 自然对数 | `log` | x:series | series | 无 |
| 数值运算 | log(1+x) | `log1p` | x:series | series | 无 |
| 数值运算 | 平方根 | `sqrt` | x:series | series | 无 |
| 数值运算 | 符号 | `sign` | x:series | series | 无 |
| 数值运算 | 倒数 | `reciprocal` | x:series | series | 无 |
| 数值运算 | 平方 | `square` | x:series | series | 无 |
| 数值运算 | 数值截断 | `clip` | x:series | series | lower=-3.0, upper=3.0 |
| 数值运算 | 按条件选择数值 | `where` | condition:boolean, yes:series, no:series | series | 无 |
| 历史与滚动 | 历史滞后值 | `lag` | x:series | series | period=5 |
| 历史与滚动 | 历史差值 | `delta` | x:series | series | period=5 |
| 历史与滚动 | 历史变化率 | `pct_change` | x:series | series | period=5 |
| 历史与滚动 | 滚动均值 | `rolling_mean` | x:series | series | window=20 |
| 历史与滚动 | 滚动求和 | `rolling_sum` | x:series | series | window=20 |
| 历史与滚动 | 滚动标准差 | `rolling_std` | x:series | series | window=20 |
| 历史与滚动 | 滚动方差 | `rolling_var` | x:series | series | window=20 |
| 历史与滚动 | 滚动最小值 | `rolling_min` | x:series | series | window=20 |
| 历史与滚动 | 滚动最大值 | `rolling_max` | x:series | series | window=20 |
| 历史与滚动 | 滚动中位数 | `rolling_median` | x:series | series | window=20 |
| 历史与滚动 | 窗口内当前值百分位 | `rolling_rank` | x:series | series | window=20 |
| 历史与滚动 | 窗口内当前值标准分 | `rolling_zscore` | x:series | series | window=20 |
| 历史与滚动 | 滚动偏度 | `rolling_skew` | x:series | series | window=20 |
| 历史与滚动 | 滚动超额峰度 | `rolling_kurt` | x:series | series | window=20 |
| 历史与滚动 | 窗口最大值距今天数 | `rolling_argmax` | x:series | series | window=20 |
| 历史与滚动 | 窗口最小值距今天数 | `rolling_argmin` | x:series | series | window=20 |
| 历史与滚动 | 线性加权均值 | `linear_decay` | x:series | series | window=20 |
| 历史与滚动 | 指数加权均值 | `ema` | x:series | series | window=20 |
| 历史与滚动 | 滚动分位数 | `rolling_quantile` | x:series | series | window=20, q=0.5 |
| 历史与滚动 | 滚动相关系数 | `rolling_corr` | x:series, y:series | series | window=20 |
| 历史与滚动 | 滚动协方差 | `rolling_cov` | x:series, y:series | series | window=20 |
| 横截面 | 横截面百分位排名 | `cs_rank` | x:series | series | 无 |
| 横截面 | 横截面标准分 | `cs_zscore` | x:series | series | 无 |
| 横截面 | 横截面去均值 | `cs_demean` | x:series | series | 无 |
| 横截面 | 横截面绝对值归一 | `cs_scale` | x:series | series | 无 |
| 横截面 | 偏离横截面中位数 | `cs_median_distance` | x:series | series | 无 |
| 横截面 | 横截面分位截尾 | `cs_winsorize` | x:series | series | tail=0.05 |
| 比较条件 | 大于 | `greater` | x:series, y:series | boolean | 无 |
| 比较条件 | 大于等于 | `greater_equal` | x:series, y:series | boolean | 无 |
| 比较条件 | 小于 | `less` | x:series, y:series | boolean | 无 |
| 比较条件 | 小于等于 | `less_equal` | x:series, y:series | boolean | 无 |
| 比较条件 | 等于 | `equal` | x:series, y:series | boolean | 无 |
| 比较条件 | 不等于 | `not_equal` | x:series, y:series | boolean | 无 |
| 比较条件 | 数值有效 | `is_finite` | x:series | boolean | 无 |
| 比较条件 | 数值缺失 | `is_missing` | x:series | boolean | 无 |
| 比较条件 | 落在闭区间内 | `between` | x:series | boolean | lower=0.0, upper=1.0 |
| 逻辑条件 | 条件同时成立 | `and` | x:boolean, y:boolean | boolean | 无 |
| 逻辑条件 | 条件任一成立 | `or` | x:boolean, y:boolean | boolean | 无 |
| 逻辑条件 | 条件恰有一个成立 | `xor` | x:boolean, y:boolean | boolean | 无 |
| 逻辑条件 | 条件取反 | `not` | x:boolean | boolean | 无 |
| 预制因子 | 60日动量跳过5日 | `factor_momentum60` | 无 | series | 无 |
| 预制因子 | 5日反转 | `factor_reversal5` | 无 | series | 无 |
| 预制因子 | 20日低波动 | `factor_lowvol20` | 无 | series | 无 |
| 预制因子 | 20日成交额因子 | `factor_liquidity20` | 无 | series | 无 |
| 预制因子 | 固定四因子等权 | `factor_combo` | 无 | series | 无 |
| 预制因子 | 日内复权收益 | `factor_intraday_return` | 无 | series | 无 |
| 预制因子 | 日内振幅 | `factor_range` | 无 | series | 无 |
