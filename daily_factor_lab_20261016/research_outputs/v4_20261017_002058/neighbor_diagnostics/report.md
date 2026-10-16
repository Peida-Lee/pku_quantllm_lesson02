# V4参数邻域后验诊断

本处理只描述已有结果，不新增模拟，不改变选型、收益或既定readiness。2025已经观察，不能把邻域表现包装为独立确认。

邻居定义：同idea_id，window、universe、top_n、rebalance四轴恰有一轴不同，其余三轴相同；该轴所有其他档位均计入。

分母：expected=observed+failed+unknown。比例只以成功且数值有限的observed为分母；无观测时留空/JSON null，绝不填0。2024夏普和收益的可用数量分开列示。

配置1404项，参数族26个；有完整邻域观测的正收益孤立项0项，覆盖不完整但已观测邻居呈孤立特征0项。

孤立标记只表示本项2025为正而已观测单轴邻居均非正，不是显著性检验。高于邻居不自动称孤峰，同族相邻变体数量不能当作独立证据。

family_summary里的四项成功仅指既有status完整；研究pass计数照录既定readiness，不重新授予研究通过或真实市场可用标签。

文件：neighbor_diagnostics.csv逐项邻域；family_summary.csv按idea_id聚合；diagnostics.json完整规则、分母和限制。
