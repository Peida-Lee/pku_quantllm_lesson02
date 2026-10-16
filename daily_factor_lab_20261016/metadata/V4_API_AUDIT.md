# V4 本机API独立审阅与隔离HTTP核验

本任务仅修改 `server.py`，新增 `tests/test_v4_api.py`。六个冻结计算文件没有修改；没有重启真实服务、访问真实服务端口、读取运行中的progress/lock/队列、启动Market、原包读取、V3研究或新V4研究。所有计算调用、子进程和后台任务线程都被mock；HTTP请求发送到独立的127.0.0.1随机端口。

## 发现与修复

1. **遗留busy永久阻塞**：原GET status只修响应副本，STATE仍busy，后续POST继续409。新增统一状态协调；已结束且明确绑定study的旧任务可恢复终态，其余失联任务标记interrupted并释放busy，不伪造完成。
2. **API与CLI身份混用**：原progress会把当前CLI研究硬贴旧API job_id。新增本服务LOCAL_JOBS身份集合：本机后台线程保留接受时的API ID；CLI研究使用自己的study ID。只有本服务尚存的任务可按启动时间首次绑定，重启后未绑定旧API任务不借用新CLI结果完成。API响应、409冲突及完成状态包含任务身份。
3. **queued旧进度**：本次API任务尚未绑定研究时，旧complete进度转换为本次queued/0进度，不返回旧study ID。子进程完成而服务还在验收/构建时仍为running；构建时building；本服务任务完成后才complete。
4. **重启状态**：从server_job.json恢复时统一协调CLI锁与任务绑定；CLI仍活跃则继续busy。JSON非对象/不可读不会被当作完成。后台线程启动失败会记录failed并清busy。
5. **未验收与版本混用**：V4激活只读一次latest研究，显式要求snapshot_ready、validation.passed、非pilot、冻结code_sha256与当前计算依赖完全一致。候选必须numeric_pass；不因其收益差而拒绝复核。错版本明确提示不能声称复现。V4作业完成后也通过相同门槛，未验收不得make_snapshot/build。
6. **冻结单候选配置**：从所选研究同一份协议读取2025区间、股票池、资金、费用/容量和退出窗口，再注入该候选持股数/频率。返回source_study_id和experiment_id，避免混用第二次latest读取。
7. **早期参数拒绝**：保留旧扁平config接口；嵌套config时只允许config/strategy外层字段。未知Config字段、非有限数、布尔数值、legacy+wind_down_days/清算、无真实ST字段的strict政策在启动线程前拒绝。V4 workers/retry_failed等严格类型检查保留。AI确认也不再静默忽略未知字段。
8. **快照陈述准确**：无普通回测latest时不调用构建，并明确“未生成仪表盘快照”。新增snapshot_built字段，仅V4实际构建成功后置true。
9. **积木包并发**：原PACK_LOCK已包住读取、合并校验及原子写入，无需重写。用两个并发同ID请求证明串行写入、返回相同存储ID且只留一个JSON条目。
10. **冻结源身份**：激活候选和结果构建前调用runner既有source_identity helper，必须与protocol.source_identity的指定原包路径/大小/SHA256一致；源被替换或环境变量指向其他包均拒绝。此检查不放入状态/进度轮询。隔离测试仅mock helper，未真实读取或重算原ZIP哈希。

## 核验结果

执行 `python tests/test_v4_api.py`：**16/16通过**，测试体约1.52秒。之后针对新增“子进程完成不能提前完成API任务”分支执行：

`python -m unittest discover -s tests -p test_v4_api.py -k child_completion -v`

该项 **1/1通过**，约0.61秒。因此17项均有通过证据，分两次执行；没有为了扩大数量重复运行已通过测试。JSON见同目录 `V4_API_AUDIT.json`。

后续仅为新增源身份门槛定向执行 `python -m unittest discover -s tests -p test_v4_api.py -k source_ -v`，**3/3通过**，约0.62秒：匹配源可激活且轮询不查源、不匹配拒绝、非法环境变量源由helper拒绝；全部通过mock，没有实际ZIP读取。现20项均有通过证据，分16+1+3三批执行。

覆盖：202/409/complete相同ID；queued旧progress；API首次绑定；CLI新研究与旧API身份分离；重启后明确绑定的完成/仍持锁/未绑定三个分支；冻结协议配置及单次latest；未验收/pilot/错版本/候选数值失败拒绝；无普通latest不声称构建；构建阶段busy；线程启动失败；参数/Origin/未知路由/JSON类型；两个同ID包并发写锁。

首次两次测试准备遇到Python 3.14 TemporaryDirectory生成目录的子路径访问拒绝，分别位于默认临时目录和workspace，均发生在HTTP用例setup之前，不能计作API失败或通过。改用workspace内普通继承ACL的唯一目录后测试通过，没有改变系统或用户ACL。正常测试fixture在结束后删除；早期无法访问的空目录不通过提升权限或ACL修复处理。

## 明确边界

- 这是Handler和作业状态逻辑的隔离HTTP测试；run_suite、研究函数、subprocess、job线程被mock，不证明真实完整队列完成、真实浏览器显示、真实后台子进程耗时或生产服务已加载新源码。
- 未重启当前服务，因此源码修复须在主任务允许的合适时点重新加载，当前长队列不受本任务影响。
- server.py不在六个冻结计算依赖中；本次修复不改变已启动研究的因子、交易、数据、协议或任何数字。
- 本服务的LOCK串行化本进程API，V4 CLI已有计算锁防止同类重复研究；其他独立进程若直接运行旧脚本，不受本服务线程锁统一调度。不能把本机HTTP mock的互斥性称为所有外部程序的全局锁。
- 重启时尚未绑定study的旧API任务保守标记interrupted；当前CLI研究仍可用其自身ID查询，不把时间更晚的研究冒充旧任务。
