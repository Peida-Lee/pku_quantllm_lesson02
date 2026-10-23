# 五日反转评估

[市场代理 β / α 报告](market_beta_alpha_20261023_203503/MARKET_BETA_ALPHA.md) · [完整五维评价](evaluation_20261023_200711/REPORT.md) · [摘要](evaluation_20261023_200711/PREVIEW.md) · [三池敏感性](evaluation_20261023_200711/POOL_SENSITIVITY.md)

2025年主池净收益37.3564%，市场代理β为0.9900，线性年化α为−0.2034%（95%CI −22.84%至22.43%，原始P=0.985946）。尚未发现明确正α；股票池、基准、执行约束和区间都会影响解释。仅使用课程feathers.zip，rf=0，非官方风险模型。

[主评价验收摘要](evaluation_20261023_200711/validation_summary.json) · [股票池验收摘要](pool_sensitivity_20261023_202110/validation_summary.json) · [β/α验收摘要](market_beta_alpha_20261023_203503/validation_summary.json) · [β/α冻结协议公开版](market_beta_alpha_20261023_203503/protocol_public.json)

本目录仅含源代码、聚合结果、图和验收摘要。原始数据及逐笔交易/持仓/每日策略账本保留在本地。P为原始P；多项回顾性敏感性不是独立确认性发现。旧本地预览已获用户审阅和发布授权。

## 源码与复现边界

依赖Python、numpy、pandas、scipy、statsmodels、pyarrow、matplotlib；沿用同仓库的daily_factor_lab_20261016引擎。run_evaluation.py生成新信息评价与主池执行目录，pool_sensitivity.py补充另外两池；verify_evaluation.py和verify_pool_sensitivity.py独立验收。market_beta_alpha.py读取上述已验收的本地daily.csv，通过原ZIP生成代理和回归，verify_market_beta_alpha.py独立重建成员与统计，render_market_beta_alpha.py仅渲染已保存聚合结果。源代码中的BASE/POOL常量对应本次冻结目录，复跑新目录时须显式指定或调整这些输入；不应假定新目录自动替代冻结输入。

公开聚合文件足以核查表格与图形；从原始行情重算需要用户自备获授权feathers.zip和本地生成的逐日账本。公开版本省去本机绝对路径和本地明细链接，未更改指标数值。Python算法源码原字节保留。完整性核对不经网页版Pro。
