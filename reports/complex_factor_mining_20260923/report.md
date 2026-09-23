# 更复杂的股票因子与集成模型：第二轮结果

状态：部分完成，缺失项见审计文件。本轮使用此前已经研究过的历史样本，属于重复历史探索。
预算：2026-09-23北京时间12:17:17—17:17:17；报告生成时已用256.1分钟。

公共公式实际评分60,000条；登记120,000条来源记录，111,891个唯一表达式；精筛12,055条。
各折新因子数：{'test_2013_2014': 192, 'test_2014_2015': 192, 'test_2015_2016': 192, 'test_2016_2017': 192, 'test_2017_2018': 192, 'test_2018_2019': 192, 'test_2019_2020': 192, 'test_2020_2021': 192, 'test_2021_2022': 192, 'test_2022_2023': 192}。
传统模型内层拟合360次、最终模型168个；AutoGluon成功重拟合40/41个预测器。
AutoGluon候选任务完成51/80个，其中失败0个；未完成或失败的计划任务不能计作成功。
独立重载核验208个最终模型，完整向量重算通过1871/1871条入选表达式；14887条发现期选择决策重放通过。

## 主测试结果

仅合并2014–15、2016–17、2018–19、2020–21、2022–23五个不重叠主折；其他五折作为窗口敏感性分析。日均排序相关衡量预测排序与随后收益排序的一致程度，不是收益率。

验证期选出的组合：主测试日均排序相关为0.07566，覆盖2419个交易日、5个主折。

|方案|日均排序相关|主折数|测试日数|
|---|---:|---:|---:|
|control_original80_lgbm|0.07195|5|2419|
|control_previous_lgbm|0.07487|5|2419|
|control_complex_lgbm|0.07543|5|2419|
|control_original80_xgb|0.07236|5|2419|
|control_previous_xgb|0.07525|5|2419|
|control_complex_xgb|0.07498|5|2419|
|ag_selected_complex__medium_quality|0.07253|5|2419|
|ag_selected_complex__high_quality|0.07519|4|1938|
|validation_selected_ensemble|0.07566|5|2419|

## 固定条件下的新增因子贡献

|比较|相关差|HAC 95%区间|20日块重采样区间|
|---|---:|---|---|
|control_previous_ridge minus control_original80_ridge|0.00838|[0.00422284803725801, 0.01254509398131795]|[0.00390767439332581, 0.012821532224707027]|
|control_complex_ridge minus control_previous_ridge|0.00390|[0.001810426743873857, 0.00599855861824967]|[0.0018790918533780582, 0.005827342191657063]|
|control_previous_lgbm minus control_original80_lgbm|0.00292|[0.0006444837364277074, 0.005194189539440588]|[0.00036529992237507327, 0.005328747643998962]|
|control_complex_lgbm minus control_previous_lgbm|0.00056|[-0.001263711163651413, 0.002381311239957139]|[-0.0011572911481424041, 0.0023775689927513976]|
|control_previous_xgb minus control_original80_xgb|0.00289|[0.0006928899646849447, 0.00508374478076592]|[0.000515774676209452, 0.005127607196945248]|
|control_complex_xgb minus control_previous_xgb|-0.00027|[-0.0019813624273268913, 0.0014453114534048826]|[-0.0019212733082959487, 0.0013726882604842288]|
|ag_raw_complex_medium_quality minus ag_raw_previous_medium_quality|0.00007|[-0.0019218994693172937, 0.002052017338919649]|[-0.0018951837219064473, 0.0018326398214593503]|
|ag_raw_complex_high_quality minus ag_raw_previous_high_quality|-0.00084|[-0.003841056611941884, 0.0021669800084825677]|[-0.00398406101019214, 0.0022819161121841384]|

## 训练与资源

XGBoost使用直方图树算法；传统模型比较缩尾标准化、稳健标准化和当日截面秩。公式搜索含滚动相关、协方差、斜率、条件与多层非线性组合，发现期演化不接触模型验证年和测试年。
AutoGluon实际预处理配额为['raw', 'rank']，按剩余时间冻结；两档采用官方1.4.0的medium_quality和high_quality配置。high_quality使用5折袋装和1层堆叠，内部随机袋装只发生在历史训练集内；实际成功模型与跳过/失败项见每个预测器的leaderboard和model_failures文件。
为保护时间验证边界，首次训练禁用自动全量重拟合、保留袋装模型并开启袋装留出；先保存验证预测，冻结预处理、家族及15种等权排序组合的选择，再显式重拟合训练加验证数据。重拟合模型单独映射，不用其验证成绩。
本轮CPU利用率：{"search_resources.csv": {"mean_task_cpu_percent": 53.863512005121116, "peak_task_cpu_percent": 88.6687458684971, "fraction_samples_ge80pct": 0.6012269938650306, "minimum_available_gib": 855.6667404174805, "samples": 163}, "model_resources.csv": {"mean_task_cpu_percent": 77.31506285961034, "peak_task_cpu_percent": 100.07792063739258, "fraction_samples_ge80pct": 0.7918050941306756, "minimum_available_gib": 662.5734939575195, "samples": 1806}, "classical_resources_v3.csv": {"mean_task_cpu_percent": 65.03561092187859, "peak_task_cpu_percent": 80.95856311052832, "fraction_samples_ge80pct": 0.02304147465437788, "minimum_available_gib": 662.5089378356934, "samples": 434}, "classical_resources_v2.csv": {"mean_task_cpu_percent": 0.0, "peak_task_cpu_percent": 0.0, "fraction_samples_ge80pct": 0.0, "minimum_available_gib": 750.5424690246582, "samples": 1}}。80%是使用目标，低于目标的阶段如实报告，不通过无效运算增加负载。

## 解释边界

没有根据当前外层测试成绩重选因子、预处理或融合组合；测试表现不佳也保留。历史年份已反复使用，因此结果仍需要新时期样本验证；常规区间不校正全部自适应公式搜索过程。
股票日线价量资料足以扩展价量公式，不能据此声称覆盖财务、新闻、分析师、逐笔成交或盘口因子。标签为下一交易日收盘到第三交易日收盘收益，分段清除终点越界标签。价差指标没有扣除交易成本、涨跌停、停复牌、退市或实际成交约束，不能解释为可执行净收益。

## 可复核文件

- primary_model_summary.csv、incremental_comparisons.json：主结果与固定输入比较。
- fold_metrics.csv、all_daily_metrics.csv：全部逐折、逐日表现。
- selected_formula_catalog.csv、all_selected_factor_test_metrics.csv：所有入选公式及其测试结果。
- VERIFIED.json、FORMULA_REPLAY.json、resource_summary.json：核验与实际资源。
- 完整输出、模型、行级预测和失败证据保留在本地与计算服务器的原始交付目录。
- 本地归档包含报告、汇总表、冻结配置、公式、模型清单和复现代码；体积较大的模型与预测保留在服务器。
