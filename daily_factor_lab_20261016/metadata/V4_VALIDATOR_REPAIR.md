# V4 冻结协议容器比较修复

研究目录：`research_outputs/v4_20261017_002058`。首次验收的 `validation.first_attempt.json`、`result.first_attempt.json`、`progress.first_attempt.json` 由主代理保留。本修复不覆盖这些文件。

## 失败证据与原因

首次独立验收共5675项检查，仅 `frozen_protocol_matches_disk=false`；5616个主情景全部验收通过，19项详细账本全部通过，运行失败0，详细重建最大金额误差 `9.5367431640625e-07`。

实读发现 `protocol.json` 与 `result.first_attempt.json['protocol']`、`result_pending_validation.json['protocol']` 在JSON加载后完全相同，没有冻结协议内容差异。静态追溯：`research.py` 的 `PERIODS` 值使用tuple，`research_v4.py` 构造运行内协议时直接指定 `periods=PERIODS`；JSON序列化把tuple写成数组，重新加载后成为list。首次验证器直接用Python字典相等判断，因此如下三个值的容器类型不等：

- `protocol.periods.selection_2024`：内存tuple、落盘list；内容均为2024-01-02至2024-12-31。
- `protocol.periods.evaluation_2025`：内存tuple、落盘list；内容均为2025-01-02至2025-12-31。
- `protocol.periods.assignment_2025_2026sep`：内存tuple、落盘list；内容均为2025-01-02至2026-09-30。

仅将已落盘协议的这三个值恢复tuple，即可重现原Python比较False；经修复后的容器规范化后比较True，无需改变任何日期、配置、hash或指标。

## 修复范围与验证

仅修改 `verify_v4.py`：新增 `json_arrays()`，递归将tuple/list表示为JSON数组，再比较落盘协议。字典键、字段是否存在、数组顺序、标量及数值精度全部保留；没有采用会把非法数值清成null或把键转字符串的通用清洗器。固定金额容差仍为0.001，比例容差仍为1e-10。

在 `tests/test_verify_v4.py` 增加两项最小回归：内存tuple协议可通过完整小型产物验收；日期、1e-15数值变化、源码hash、缺字段、数组顺序变化仍被拒绝。连同已有分析性/破坏性产物测试共8项全部通过，命令：

```powershell
python -m unittest discover -s tests -p test_verify_v4.py -v
```

测试使用工作区新建小型fixture，不运行模拟，不读取真实行情ZIP。额外对真实研究仅作JSON只读核查，并核实当前六个冻结计算文件hash仍与协议一致：`lab.py`、`execution_metadata.py`、`research.py`、`strategy_blocks.py`、`v4_factors.py`、`research_v4.py`。

本代理未改原数据、协议、records、daily/trades/orders/holdings、研究指标、研究result/validation或通过标记；未启动全研究验收或重跑模拟。完整独立验收由主代理使用原落盘pending/records单次重新执行；本说明不预先宣称重验收通过。
