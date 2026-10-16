# V4 正常应用入口独立审阅与待执行验收

本次只读计算源码和已有测试；新增标准库产物比较工具。未读取正在运行的V4研究队列、进度、活动或结果，未重跑计算，未修改六个冻结计算文件。下面命令须等主代理确认研究已完成且独立验收通过后执行。本说明不表示应用复现已运行或通过。

## 入口与同源关系

`run_experiments.py --config <完整配置> --strategy <策略JSON> --no-build` 经 `Config.from_dict()`、`validate_strategy()`、`compile_strategy()` 后，调用冻结的 `lab.simulate()`。V4积木的 `_evaluate_uncached()` 调用 `v4_factors.compute_factor()`；研究器使用同一 `FactorEngine.compute()`。两者均在最终资格掩码内保留有限值，保持原始带方向因子分数，方向必须为 `desc`，不能再额外取反或加rank。

应用按顺序运行 `base=1`、`gross=0`、`stress=2`。研究的2025基线对应应用base；研究双费对应应用stress。stress是全部费率和目标滑点乘2后按价格刻度重新执行，不应通过基线收益减去一倍成本来推算。

默认 `config.json` 使用legacy模型、100万元、较低成交额/活跃日门槛，且未启用退出；不等于V4研究。必须完整导出目标记录的 `config`，保留10亿元、`daily_conservative`、`price_proxy`、退出20日、清算、历史门槛、股票池及容量约束。策略selection会覆盖配置中的top_n和rebalance，所以二者也必须一致。研究capacity场景另改两个参与率为0.001，不在本次基线/双费映射范围。

## 最小准备与运行命令

以下 `<...>` 是待研究完成后确定的真实路径/ID，不是可直接粘贴的占位文件名。工具不会挑选策略；由主代理按最终研究结果指定候选。

```powershell
Set-Location D:\quant\daily_factor_lab_20261016
python check_v4_app_consistency.py prepare --protocol "<最终study目录>\protocol.json" --base-record "<最终study目录>\runs\<experiment_id>\evaluation_2025\base\record.json" --detailed-base-record "<最终study目录>\detailed\<experiment_id>\evaluation_2025\base\record.json" --output "<全新验收目录>"
```

若该候选不在研究预定detailed抽样内，去掉 `--detailed-base-record`；不得补造已存在完整逐笔核验的表述。prepare要求成功base/stress记录、候选与冻结grid吻合、全部配置与冻结协议吻合、当前六个计算源码hash吻合。它只生成 `config.json`、`strategy.json`、`manifest.json` 和 `launch_after_completion.ps1`，不读ZIP、不模拟。

准备生成的最小策略是：

```json
{"format":"daily-factor-strategy","version":1,"name":"<候选名称>","score":{"op":"v4_<factor_id>","args":[],"params":{}},"selection":{"top_n":50,"rebalance":"monthly","direction":"desc"},"packs":[]}
```

其中factor_id、top_n、rebalance均从目标冻结候选导出，示例50/monthly不作替代。真正运行时只需：

```powershell
powershell -File "<验收目录>\launch_after_completion.ps1"
```

生成的启动器调用正常 `run_experiments.py` CLI，明确指定原 `D:\quant\feathers.zip`，数值库及Arrow各1线程，通过现有Windows拓扑函数保留两个性能核心；无法建立保留时失败关闭。它仍会自动运行gross、IC、参考曲线与快照刷新。`--no-build`只跳过前端构建，不跳过这些步骤，也不阻止读取研究快照。因此研究运行期间禁止执行此启动器。

正常应用输出新 `outputs/run_...` 后执行：

```powershell
python check_v4_app_consistency.py compare --manifest "<验收目录>\manifest.json" --app-run "D:\quant\daily_factor_lab_20261016\outputs\<新run_id>" --output "<验收目录>\comparison.json"
```

compare无计算，仅读取显式路径与冻结源码；失败返回非0并保存 `passed=false`。通过要求如下：

1. 配置、完整原生策略、准备时的研究record/protocol内容、当前计算hash均一致；应用完整且无失败。
2. base/stress的daily.csv全部列、全部行、原有行序及17位序列化值逐单元精确一致，日期/现金/NAV/费用/换手/持仓等无省略；13个共有绩效指标按JSON数值精确一致。
3. 基线的终态、拒单计数、执行诊断完全一致。
4. 传入detailed基线时，额外对比daily/trades/orders/holdings全部行列。

应用不导出stress逐笔交易/订单/残仓，不能报告双费逐笔一致。应用产物本身未记录运行时源码hash：工具比较的是当前代码与冻结协议，不能把它写成独立的运行时版本证明。工具不重算源ZIP的hash；本次无文件传输，不进行新的传输完整性核对。最终研究的独立数值验收仍应作为前提，逐行一致只证明同一声明历史模型中的复现一致。

## 现有测试覆盖与剩余风险

已审阅现有测试代码，无重跑：`test_v4_integration.py`覆盖78项原始切片因子与全部注册积木的有效域/数值精确一致、未来数据扰动不改变训练前缀和三个基线的实际目标订单；`test_v4_factors.py`覆盖方向、公式、缺失、零/负/平坦分母、rank、缓存、非法ID/形状；`test_execution_precision.py`与`test_v4_execution_audit.py`覆盖前收目标冻结、实际刻度成交额、费用/容量、T+1、退出锁单/残仓/减记区分、因子未知参考、待买资格重检和非法费用倍数。

这些已有检查不能代替完整正常应用产物的事后逐行比对。正常应用直接CLI没有研究器的CPU保留包装，lab导入还会设置Arrow为4计算/2IO线程；因此验收应使用生成的受控启动器。原始P、回顾性探索、历史ST缺失、竞价未核实、合成总回报交收与事后日线容量约束等边界继续保留。
