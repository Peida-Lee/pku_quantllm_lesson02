# V4 因子规格与代码审阅

本文件审阅 `research.py`、`strategy_blocks.py` 及 `lab.py` 的现有接口，并记录独立 `v4_factors.py` 的设计和合成核验。未修改上述三个旧文件，未重新运行课程原包全量回测，也没有发布操作。

## 计数、方向与输入边界

- 因子库含 **78 个参数规格、26 个参数族**。默认股票池3档、持股数20/50/100、周/月调仓，形成 **1,404 个配置候选**。这些是配置计数，不是1,404个独立idea；族之间也存在经济和统计相关性。
- 1/5/10日收盘收益固定用反转方向，20/60/120日固定用动量方向；未把相同窗口的正反符号两次计数。每个方向是事前研究假设，不是收益结论，不能按2025结果修改后冒充预定方向。
- 只读取课程原ZIP七矩阵：open/high/low/close/amt/num_trades/cumadj；不访问外部服务、不推断股票名称/真实ST状态，不使用交易账户。
- C/O/H/L为原价乘以正累计复权比例。每个证券以**第一个历史有效复权因子**归一化；比例不使用末日因子。只用同证券的比率、波动和位置等量，不横截面比较复权价格水平。
- A=amt，按人民币元解释仅是一项未外部核实的单位假设；N=num_trades是成交**笔数**，不是成交股数，未据此构造VWAP、股数换手率或市值因子。A/N只能称每笔金额代理。
- t日收盘计算，最早t+1日执行。隔夜缺口因子在t收盘已知，仍不能倒用于t开盘订单。cumadj是包内最终提供的历史序列，不能单靠本库证明供应商发布当时版本可得性。

## 接口契约

`FACTOR_SPECS`按稳定ID保存JSON兼容元数据；`catalog()`返回深拷贝。字段包括id、中文name、idea_id、family、window、min_history、required_history_rows、direction、raw_formula、formula、required_fields、parameterization、validity、signal_timing、boundaries、counting_note。公式中的w由window给定；direction应用前为raw_formula，最终带符号公式为formula。

`FactorEngine(market, max_cache=1)`读取 `market.raw`。七字段必须同为非空二维[date,symbol]形状；日期/代码的实际对齐继承现有Market加载器的逐字段校验，不凭形状相同推断日期一致。

- `compute(id, eligible=None)`返回float64[T,N]，已经统一为越高越优，无效值为NaN。可选eligible只掩码，不改变缓存。返回缓存只读，禁止调用方原地覆盖。
- `score(id, eligible, rank=True)`只对最终eligible池内有限值做同日平均百分位rank；缺失不进入分母，同日少于2个有效证券则整行NaN。`rank=False`返回掩码后的原始带符号值。
- 当前78项均为时间序列单因子，没有池相关的中间排名；因此root可直接 `np.where(eligible, engine.compute(id), np.nan)`交给现有确定性股票排序。将来若增加rank组合，必须先在最终池内排序，不能把全市场预排序当成池内排序。
- 最多缓存0/1/2个最终因子矩阵，另复用懒计算的原语；这不是“进程总共只占两个矩阵”。`clear()`释放结果和原语。全量日矩阵约72MB/个，七行情和若干原语也占内存；root应按因子分组逐组处理并限制进程数，不一次保存78项。
- `candidate_grid()`输出experiment_id/factor/name/universe/top_n/rebalance。池ID匹配旧研究的all_liquid/main_liquid/liquid1000，但选股资格由runner截至信号日生成；库不把全样本末日存活表当股票池。

## 规格表

下面w=10/20/60的每行各代表3个参数规格；其余9项单列。窗口均包括t，std采用ddof=1，10%分位用线性插值。required_history_rows包含收益或缺口的额外前一行，跳过5日动量需要w+6行。

| ID前缀 | 中文含义/最终带符号定义 | 窗口/最少源行 |
|---|---|---|
| reversal | −(C[t]/C[t−w]−1) | w=1/5/10；w+1 |
| momentum | C[t]/C[t−w]−1 | w=20/60/120；w+1 |
| momentum_skip5 | C[t−5]/C[t−w−5]−1 | w=20/60/120；w+6 |
| trend_efficiency | (C[t]/C[t−w]−1)/sum_w(abs(r)) | w=10/20/60；w+1 |
| return_to_risk | mean_w(r)/std_w(r) | w=10/20/60；w+1 |
| lowvol | −std_w(r) | w=10/20/60；w+1 |
| downside_risk | −sqrt(mean_w(min(r,0)^2))，分母是全部w日 | w=10/20/60；w+1 |
| tail_return | quantile_w(r,0.1) | w=10/20/60；w+1 |
| positive_fraction | mean_w(1[r>0]) | w=10/20/60；w+1 |
| intraday_trend | mean_w(C/O−1) | w=10/20/60；w |
| overnight_reversal | −mean_w(O[t]/C[t−1]−1) | w=10/20/60；w+1 |
| lowrange | −mean_w((H−L)/C) | w=10/20/60；w |
| closing_position | mean_w((2C−H−L)/(H−L)) | w=10/20/60；w |
| high_position | C[t]/max_w(H)−1 | w=10/20/60；w |
| channel_position | (C[t]−min_w(L))/(max_w(H)−min_w(L)) | w=10/20/60；w |
| mean_reversion | −(C[t]/mean_w(C)−1) | w=10/20/60；w |
| range_surprise | −当日振幅/mean_w(振幅)，均值包含当日 | w=10/20/60；w |
| gap_volatility | −std_w(O[t]/C[t−1]−1) | w=10/20/60；w+1 |
| intraday_volatility | −std_w(C/O−1) | w=10/20/60；w |
| liquidity | log1p(mean_w(A)) | w=10/20/60；w |
| amount_stability | −std_w(log1p(A)) | w=10/20/60；w |
| amihud | −mean_w(abs(r)/(A/1e8))，金额冲击代理 | w=10/20/60；w+1 |
| return_amount_corr | −corr_w(r,log1p(A)) | w=10/20/60；w+1 |
| amount_trend | log(mean_5(A)/mean_w(A)) | w=10/20/60；w |
| trade_intensity | log1p(mean_w(N)) | w=10/20/60；w |
| average_trade_value | log1p(mean_w(A/N))，先逐日A/N再平均 | w=10/20/60；w |

`trend_efficiency`是表中定义的复合收益/绝对单日收益路径比，不是Kaufman效率比，也不保证在[−1,1]内。`return_to_risk`不是净交易夏普，没有扣费、年化或无风险收益。`high_position`与`channel_position`含当日高低，仅用于次日信号。零成交额可使liquidity合法为0；A=0的Amihud分母不合法。N=0时平均每笔金额无定义，不补0。零波动lowvol合法为0，但零方差的收益波动比和相关系数无定义。

## 审阅发现与处理

1. **滚动样本**：旧端点动量只需要两个端点，即使中间缺价仍能给值。V4额外要求端点之间每个源日有效；跳过5日动量也保守要求被跳过的五行有效。收益波动w项要求w+1源行。所有滚动量使用min_periods=w，缺失后不靠partial window提早恢复。
2. **非法输入**：旧研究部分公式可把非正原价/负amount带进计算。V4将非正价格/cumadj、负A/N转为NaN，不修改原矩阵；范围量要求H≥C≥L；high_position要求H≥C。H=L时振幅可为0，但收盘位置分母0而无效。
3. **未来信息**：不使用shift负数、center窗口、bfill、末日归一化、全样本价格均值或未来标签。未来扰动与截断前缀对每项分别核验。现有strategy_blocks已对偏度/峰度全列中心化问题作特殊处理，V4没有新增这类高阶矩因子，避免把代码量当因子质量。
4. **除权**：使用一致复权OHLC；拆股原价减半而cumadj加倍不产生新增收益/缺口/振幅信号。若cumadj本身错误或缺失，本库不能修正公司行为；复权总回报份额仍不是实际交收。
5. **横截面排序**：缺失不参与rank，最终股票池的平均百分位处理并列。现有引擎原始分数排序只改变呈现尺度，不应额外在全市场先rank再做非单调组合。单一因子的方向不会由池内rank自动反转。
6. **成交额和笔数**：A/N不能当成交均价，abs(r)/A只是不含流通市值的金额冲击代理。资金和A的单位若不一致，容量与金额型解释同时失效；因子有限不代表实盘容量已验证。
7. **过拟合**：1,404配置不是扩大独立样本量。必须冻结候选与方向、保留所有失败项，用训练/验证/时间隔离选择。2025已在V1/V3观察，不能重新冠名未触碰测试集。仅报告原始P，同时说明多重探索与选择偏差；不得因原始P较小隐去候选数。

## 已完成的核验与剩余边界

执行：`python -m unittest discover -s tests -p test_v4_factors.py -v`。**11/11通过**，测试体约2.9秒（小型固定随机合成数据，非课程全量研究）。覆盖：

- 78规格/26参数族、1,404无重复ID候选；目录深拷贝。
- 所有规格完整输入后能输出有限信号，历史不足严格NaN。
- 14项独立手算公式，含下行风险、尾分位、位置、金额冲击、相关及每笔金额。
- 全78项未来扰动与截断前缀一致；一致拆股复权不制造信号。
- 中间缺价、缺复权、负A、零A/N、平价区间、零方差的退化行为。
- 最终池排序、缺失分母、并列平均rank及少于2个名字的拒绝。
- 缓存上限/只读性/clear，不修改源矩阵。
- 在完整有效的合成输入上，7项已有V3基线逐格等价：reversal_w001/w005、momentum_w020、momentum_skip5_w060、lowvol_w020、liquidity_w020、lowrange_w020。

本次核验确认实现的公式、因果窗口与退化行为，没有确认真实原包的每项覆盖率、全候选回测完成、未来收益、真实ST筛选、真实成交或10亿元容量。原包覆盖率和后续研究须由主任务另行记录；不把11个测试当成1,404个实盘有效策略证明。
