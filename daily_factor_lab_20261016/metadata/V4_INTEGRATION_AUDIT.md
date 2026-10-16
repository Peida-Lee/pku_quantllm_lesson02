# V4 集成独立审阅与原包小切片核验

本审阅针对 `research_v4.py` 的冻结/恢复/队列/等价签名/标签及资源策略，并通过原课程ZIP的小切片检查所有78个规格与78个策略积木入口。禁止发布；本任务没有完整候选网格回测，没有原包再次完整性哈希，没有生成或传输原始行情副本。

## 已报告给主任务的问题

1. **写盘失败伪成功**：早期 `_run_one` 在写daily/逐笔文件前置success，except不改failed，会让恢复直接跳过未落盘结果。应全部写完后再success，失败明确failed。
2. **冻结依赖不完整**：早期CODE_FILES遗漏research.py，而配置、股票池、时间区间实际从research.py导入；恢复时config_for还读取当前全局而非协议快照。应冻结完整计算依赖并使用协议内配置。
3. **坏record中断队列**：早期旧record JSON在try外读取；group外异常分支又直接读同一个坏JSON，会再次抛出并终止父队列。应对单项坏文件保护、保留并标记failed，继续其他项。
4. **换源误恢复**：早期允许FACTOR_DATA_ZIP换源却把协议写成固定原包。应明确拒绝非指定源，冻结源身份并在恢复校验。此次小切片读取不重新做原包完整性哈希；主任务的源身份记录单独管理。
5. **等价签名过细**：等权目标对篮子内分数排序不敏感，签名应对选定符号排序后编码。该签名仅是训练期调仓选股计划等价，不证明待成交重试、成交路径或净收益等价。
6. **跨半年标签缺失**：早期all(已有半年收益>0)加非空检查会让只有一个半年的结果通过“两个半年正收益”。应要求H1/H2均在。

上述是首次代码快照的问题，主任务可能已修复；本文件不把审阅记录误称为当前仍未修复。相关根文件由主任务拥有，本任务未擅自修改。

后续只读代码复查确认：主任务已将research.py/strategy_blocks.py加入计算hash、禁止环境变量换源、冻结源身份、config_for读取协议periods/universes、写盘完成后置success且except明确failed、对坏record保留并标记失败、按篮子排序生成签名、要求H1/H2齐备。这些是源码复查结果；故障恢复路径没有在本次小切片数值测试中逐个注入磁盘故障重现。

## 小切片协议和证明范围

`tests/test_v4_integration.py` 逐字段在Arrow层只选48个等间距证券列，随后保留2023-06-01至2025-03-31源日期，七字段仅各读取一次，确认切片日期/证券轴相同。Market不读取全市场矩阵；源文件不改写。预留两个性能核心、数值库线程1、Arrow CPU/I/O线程1。

- 全78项输出覆盖计数；修改2025全部七字段，不得改变2024及以前分数有限性/数值和2024选股计划签名。
- 对至少7个旧基线，仅在V4完整合法输入产生有限值的位置与原research.py定义比较。V4更严格的缺失mask本来就可能比旧定义少，不能将旧端点公式越过缺失窗口当等价标准。
- 全78个v4_积木编译成功，并且最终股票池mask/数值与FactorEngine完全相同；未知op及无参数op被额外传参均拒绝。
- 记录原切片自然缺失，再注入单点缺价/金额/笔数，证明所有相关窗口保留NaN，不补0。
- 3个既有基线各模拟同一2024小股票池两次，2025扰动前后的所有实际订单（包括target_adjusted_units）和日路径精确一致。这是有原始数据的小切片因果回归检查，不是全部候选成交验证。

## 资源、标签与恢复的剩余边界

每worker会加载自己的Market七行情和原语，max_cache=1只限制最终分数缓存，不是进程内存只占一个矩阵。最多6worker是否适合本机，须以可用内存和pilot实测为准。拓扑预留失败不应仍宣称预留成功。Arrow导入旧lab后会被设成4/2线程，worker初始化和此测试显式重置1/1。

成功record还应与冻结配置/源/代码绑定，恢复时确认要求的实际落盘文件存在且可读；仅存在status=success不足以证明可恢复。broken process pool/worker初始化失败时，应记录未执行项并保留明确可恢复边界；不能把所有未跑项伪成完成。详细审计group失败应保留候选ID，便于修复。

数值验收、研究门槛和市场可执行性必须分开。原始P并不消除1404组搜索的选择偏差；2025已观察，所有V4结果都属于同源回顾性探索。小切片覆盖率不能外推为全市场/全日期覆盖率，切片候选发生亏损或未退出也不意味着代码因果检查失败。

## 实际执行结果

未全部通过。以下为原包小切片的实测记录：

```json
{
  "source": "D:\\quant\\feathers.zip",
  "source_bytes": 159096263,
  "rows": 443,
  "symbols": 48,
  "first_date": "2023-06-01",
  "last_date": "2025-03-31",
  "resource": {
    "method": "Windows_CPU_set_topology",
    "reserved_logical_processors": [
      0,
      1,
      2,
      3
    ],
    "allowed_logical_processors": [
      4,
      5,
      6,
      7,
      8,
      9,
      10,
      11,
      12,
      13,
      14,
      15,
      16,
      17,
      18,
      19
    ],
    "cpu_sets": [
      {
        "logical": 0,
        "core": 0,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 1,
        "core": 0,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 2,
        "core": 2,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 3,
        "core": 2,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 4,
        "core": 4,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 5,
        "core": 4,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 6,
        "core": 6,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 7,
        "core": 6,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 8,
        "core": 8,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 9,
        "core": 8,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 10,
        "core": 10,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 11,
        "core": 10,
        "efficiency": 1,
        "group": 0
      },
      {
        "logical": 12,
        "core": 12,
        "efficiency": 0,
        "group": 0
      },
      {
        "logical": 13,
        "core": 13,
        "efficiency": 0,
        "group": 0
      },
      {
        "logical": 14,
        "core": 14,
        "efficiency": 0,
        "group": 0
      },
      {
        "logical": 15,
        "core": 15,
        "efficiency": 0,
        "group": 0
      },
      {
        "logical": 16,
        "core": 16,
        "efficiency": 0,
        "group": 0
      },
      {
        "logical": 17,
        "core": 17,
        "efficiency": 0,
        "group": 0
      },
      {
        "logical": 18,
        "core": 18,
        "efficiency": 0,
        "group": 0
      },
      {
        "logical": 19,
        "core": 19,
        "efficiency": 0,
        "group": 0
      }
    ]
  },
  "numeric_threads": 1,
  "arrow_threads": 1,
  "full_grid_backtest": false,
  "archive_hash_recomputed": false,
  "actual_target_order_rows_unchanged": {
    "reversal_w005": 8498,
    "lowvol_w020": 6518,
    "lowrange_w020": 6355
  },
  "future_perturbation_all_specs": true,
  "training_signatures_unchanged": 78,
  "registered_blocks_exact_match": 78,
  "unknown_and_extra_parameter_ops_rejected": true,
  "baseline_valid_cells": {
    "dr5_reversal": 20241,
    "reversal1": 20429,
    "momentum20": 19536,
    "momentum60": 17421,
    "lowvol20": 19536,
    "liquidity20": 19583,
    "lowrange20": 19583
  }
}
```

|因子规格|2024有限单元|2024总单元|最终资格内有限单元|签名不变|
|---|---:|---:|---:|---|
|reversal_w001|11199|11616|9971|True|
|reversal_w005|11195|11616|9971|True|
|reversal_w010|11190|11616|9971|True|
|momentum_w020|11180|11616|9971|True|
|momentum_skip5_w020|11175|11616|9971|True|
|momentum_w060|11140|11616|9971|True|
|momentum_skip5_w060|11135|11616|9971|True|
|momentum_w120|11129|11616|9971|True|
|momentum_skip5_w120|11124|11616|9971|True|
|trend_efficiency_w010|11190|11616|9971|True|
|trend_efficiency_w020|11180|11616|9971|True|
|trend_efficiency_w060|11140|11616|9971|True|
|return_to_risk_w010|11190|11616|9971|True|
|return_to_risk_w020|11180|11616|9971|True|
|return_to_risk_w060|11140|11616|9971|True|
|lowvol_w010|11190|11616|9971|True|
|lowvol_w020|11180|11616|9971|True|
|lowvol_w060|11140|11616|9971|True|
|downside_risk_w010|11190|11616|9971|True|
|downside_risk_w020|11180|11616|9971|True|
|downside_risk_w060|11140|11616|9971|True|
|tail_return_w010|11190|11616|9971|True|
|tail_return_w020|11180|11616|9971|True|
|tail_return_w060|11140|11616|9971|True|
|positive_fraction_w010|11190|11616|9971|True|
|positive_fraction_w020|11180|11616|9971|True|
|positive_fraction_w060|11140|11616|9971|True|
|intraday_trend_w010|11191|11616|9971|True|
|intraday_trend_w020|11181|11616|9971|True|
|intraday_trend_w060|11141|11616|9971|True|
|overnight_reversal_w010|11190|11616|9971|True|
|overnight_reversal_w020|11180|11616|9971|True|
|overnight_reversal_w060|11140|11616|9971|True|
|lowrange_w010|11191|11616|9971|True|
|lowrange_w020|11181|11616|9971|True|
|lowrange_w060|11141|11616|9971|True|
|closing_position_w010|11012|11616|9850|True|
|closing_position_w020|10868|11616|9743|True|
|closing_position_w060|10397|11616|9371|True|
|high_position_w010|11191|11616|9971|True|
|high_position_w020|11181|11616|9971|True|
|high_position_w060|11141|11616|9971|True|
|channel_position_w010|11191|11616|9971|True|
|channel_position_w020|11181|11616|9971|True|
|channel_position_w060|11141|11616|9971|True|
|mean_reversion_w010|11191|11616|9971|True|
|mean_reversion_w020|11181|11616|9971|True|
|mean_reversion_w060|11141|11616|9971|True|
|range_surprise_w010|11191|11616|9971|True|
|range_surprise_w020|11181|11616|9971|True|
|range_surprise_w060|11141|11616|9971|True|
|gap_volatility_w010|11190|11616|9971|True|
|gap_volatility_w020|11180|11616|9971|True|
|gap_volatility_w060|11140|11616|9971|True|
|intraday_volatility_w010|11191|11616|9971|True|
|intraday_volatility_w020|11181|11616|9971|True|
|intraday_volatility_w060|11141|11616|9971|True|
|liquidity_w010|11191|11616|9971|True|
|liquidity_w020|11181|11616|9971|True|
|liquidity_w060|11141|11616|9971|True|
|amount_stability_w010|11191|11616|9971|True|
|amount_stability_w020|11181|11616|9971|True|
|amount_stability_w060|11141|11616|9971|True|
|amihud_w010|11169|11616|9960|True|
|amihud_w020|11139|11616|9947|True|
|amihud_w060|11019|11616|9872|True|
|return_amount_corr_w010|11190|11616|9971|True|
|return_amount_corr_w020|11180|11616|9971|True|
|return_amount_corr_w060|11140|11616|9971|True|
|amount_trend_w010|11191|11616|9971|True|
|amount_trend_w020|11181|11616|9971|True|
|amount_trend_w060|11141|11616|9971|True|
|trade_intensity_w010|11191|11616|9971|True|
|trade_intensity_w020|11181|11616|9971|True|
|trade_intensity_w060|11141|11616|9971|True|
|average_trade_value_w010|11170|11616|9960|True|
|average_trade_value_w020|11140|11616|9947|True|
|average_trade_value_w060|11020|11616|9872|True|

测试输出：

```text
test_actual_2024_order_target_prefix_ignores_future_2025_mutation (__main__.OriginalSliceIntegrationTests.test_actual_2024_order_target_prefix_ignores_future_2025_mutation) ... ok
test_actual_original_missingness_and_injected_hole_never_fill_zero (__main__.OriginalSliceIntegrationTests.test_actual_original_missingness_and_injected_hole_never_fill_zero) ... FAIL
test_all_78_coverage_prefix_and_2024_signatures (__main__.OriginalSliceIntegrationTests.test_all_78_coverage_prefix_and_2024_signatures) ... ok
test_all_registered_blocks_compile_with_exact_factor_validity (__main__.OriginalSliceIntegrationTests.test_all_registered_blocks_compile_with_exact_factor_validity) ... ok
test_seven_baselines_on_legal_complete_input_intersection (__main__.OriginalSliceIntegrationTests.test_seven_baselines_on_legal_complete_input_intersection) ... ok

======================================================================
FAIL: test_actual_original_missingness_and_injected_hole_never_fill_zero (__main__.OriginalSliceIntegrationTests.test_actual_original_missingness_and_injected_hole_never_fill_zero)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\quant\daily_factor_lab_20261016\tests\test_v4_integration.py", line 178, in test_actual_original_missingness_and_injected_hole_never_fill_zero
    self.assertTrue(np.isnan(engine.compute(fid)[t, j]), fid)
    ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: np.False_ is not true : overnight_reversal_w010

----------------------------------------------------------------------
Ran 5 tests in 4.916s

FAILED (failures=1)

```

## 缺失项测试修正与定向复核

首轮4项通过、1项未通过的原因是测试错误地要求当前C缺失使隔夜O[t]/C[t−1]失效；公式本来不依赖C[t]。仅修测试，额外置当前O缺失，未修改因子/策略/执行引擎。定向复跑该1项，结果如下；其余4项沿用首轮通过证据，没有重复运行。最终5项覆盖检查均通过（分两次执行），不把首轮失败隐去。

```text
test_actual_original_missingness_and_injected_hole_never_fill_zero (test_v4_integration.OriginalSliceIntegrationTests.test_actual_original_missingness_and_injected_hole_never_fill_zero) ... ok

----------------------------------------------------------------------
Ran 1 test in 2.346s

OK

```
