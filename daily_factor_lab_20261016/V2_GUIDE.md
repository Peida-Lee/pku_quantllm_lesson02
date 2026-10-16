# 第二版：可视化策略积木

## 从积木到回测

1. 打开“策略积木”，选择一个预置模板，或从行情与数值积木开始。
2. 左侧按类别或关键词找积木，拖到右侧对应类型的插槽。评分输出需要数值序列，筛选输出需要布尔条件。也可以先点插槽，再点击模块添加。
3. 修改窗口、阈值等参数，设置持仓数、调仓频率与排序方向；撤销/重做及本机草稿只影响策略文档。
4. 点击“校验策略”。未连接输入、类型不匹配、未知操作、未来滞后或超出资源边界会被拒绝。
5. 点击“运行这套策略”，等待三档费用回测完成，再点“加载结果页面”。结果按该策略的实际配置展示，并提供策略JSON和编译清单下载。

评分在t日收盘后形成，最早t+1开盘尝试成交。股票筛选积木只能在基础股票池资格上进一步筛选；不能用积木跳过同一配置下的费用、整手、T+1和执行门禁。多次交互试验属于探索，不是独立留出验证。

## 两种导入文件

**策略JSON**（`format: daily-factor-strategy`，`version: 1`）保存名称、评分树、可选筛选树、组合选择与所依赖的积木包。示例见[低波动](examples/lowvol20.json)、[动量](examples/momentum60.json)、[等权组合](examples/combo.json)、[自定义反转](examples/reversal5_custom.json)。

```json
{
  "format": "daily-factor-strategy",
  "version": 1,
  "name": "我的20日低波动",
  "score": {
    "op": "negate",
    "args": [{
      "op": "rolling_std",
      "args": [{"op": "daily_return"}],
      "params": {"window": 20}
    }]
  },
  "selection": {"top_n": 100, "rebalance": "weekly", "direction": "desc"},
  "packs": []
}
```

**积木包JSON**（`format: daily-factor-block-pack`，`version: 1`）保存可复用的组合模块。导入后出现在模块库中；策略导出会同时携带依赖包，换机器后仍可恢复。真实例子见[短期反转积木包](examples/short_reversal_pack.json)。模板中的`input`代表该积木输入，其他节点必须是已有白名单运算或包内可校验的组合。

这些JSON不是可执行脚本，也不等同于Scratch的`.sb3`。不能导入任意Python、JavaScript、网络请求、文件访问或未来行情函数。宏、未使用的隐藏模块、参数和输入类型也会校验。

## 规则、缺失与资源边界

- 目前是日频、只做多、横截面选股评分与筛选系统；不是通用程序语言或券商执行平台。
- 评分越高越优，方向选择为升序时由编译器统一反转评分。`selection`显式持仓数/频率优先于通用配置，缺省则继承。
- 数值缺失不填0，非法数值运算变为缺失；布尔缺失向下传播，最终筛选条件缺失时排除该股票。
- 滚动计算只用当前及过去；窗口须满足模块边界，通常完整窗口才有结果。最高窗口252，原始及宏展开节点上限128、深度16、JSON上限150000字节；缓存最多8个中间矩阵。
- 无法还原真实现金分红、送转股和税款，仍采用累计复权因子的合成份额；开盘成交、涨跌停、ST和缺价减记的局限全部沿用[V1报告](report.md)。
- 原始`num_trades`末日缺失，使用该字段的自定义策略会受到相应缺失影响，不会自动补值。
- 常数评分、缺少完整未来结果或有效股票不足时，RankIC可能无法估计，不能把空缺理解为0或不显著。

## 复现与接口

### AI辅助确认接口（首版，未接入真实模型）

积木页提供“AI辅助核对”入口，可辅助解释选股方向、时间窗口、筛选、调仓、费用假设与需要补充的信息。它只读，不自动修改积木或执行策略；静态类型校验始终由本地编译器决定。

- `GET /api/ai/status`：返回是否配置模型，不返回密钥。
- `POST /api/ai/confirm`：提交`{strategy, question, config}`，返回本地规则说明及可选模型意见。
- 可在服务启动环境设置`FACTOR_AI_BASE_URL`、`FACTOR_AI_MODEL`、`FACTOR_AI_API_KEY`，使用OpenAI兼容的Chat Completions接口。密钥不在网页填写、不保存在策略JSON或日志中。
- 只有点击确认后才会发送策略、问题和执行参数，不发送原始行情矩阵。远端回复是辅助意见，不等同于策略可交易、正确或获利。
- 未完整配置时明确显示“未接入外部模型：仅本地规则确认”；不能将此视为模型确认。当前交付没有配置或调用真实第三方模型。

AI接口为初版，当前没有配置或验证真实第三方模型。按用户要求停止追加测试并快速提交；既有积木、引擎与API验证记录不扩大为真实AI接入已验证。

```powershell
python run_experiments.py --config config.json --strategy examples/lowvol20.json --no-build
python -m unittest discover -s tests -v
python server.py --port 8776
```

保留原始五因子运行方式：不传`--strategy`。各次运行单独存放在`outputs/run_*/`，不会覆盖V1实验。CLI输出中有`strategy.json`、`strategy_manifest.json`、三费用情景及IC结果。

- `GET /api/blocks`：类型化模块目录、可用模板、已导入积木包。
- `POST /api/strategy/validate`：提交`{strategy: ...}`，静态校验并返回规范化文档及元数据，不读取行情。
- `GET/POST /api/strategies`、`GET /api/strategies/<id>`：策略保存与读取，文件名由服务器生成。
- `POST /api/block-packs`：提交`{pack: ...}`导入安全组合模块。
- `POST /api/run`：提交`{config: ..., strategy: ...}`运行自定义策略；原直接配置请求仍执行五因子。
- `GET /api/runs`、`POST /api/runs/activate`：选择历史实验并重建对应结果快照。

网页重建沿用已安装Codex Data插件；公共薄源码可用`python bootstrap_dashboard.py`恢复官方运行时，完整步骤见README。

## 验证边界

引擎18项、策略编译器27项单元测试覆盖手算、恶意/递归/超大AST拒绝、宏和导出导入；全部79模块默认表达式可执行，并逐模块验证“修改未来行情不改变此前输出”。低波动积木完整回测与V1逐日净值/现金相等，见`v2_engine_equivalence.json`。

接口、真实宏短区间回测、历史结果切换与浏览器拖拽/导入检查另见`api_validation_v2.json`及`dashboard/browser_validation_v2.json`。这些检查验证软件约定，不证明真实竞价、涨跌停排队成交或实盘盈利。

完整模块定义（输入类型、参数默认值和上下限）在[block_catalog.json](block_catalog.json)，表格速查见[模块清单](BLOCKS.md)。
