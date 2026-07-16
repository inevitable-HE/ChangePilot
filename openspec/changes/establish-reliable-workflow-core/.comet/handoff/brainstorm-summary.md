# Brainstorm Summary

- Change: establish-reliable-workflow-core
- Date: 2026-07-16

## 确认的技术方案

- 采用“可靠执行内核自研、Agent 规划层后续使用 LangGraph”的分层方案。LangGraph 不拥有执行状态；规划结果必须通过稳定 Schema 与安全校验后，以不可变静态 DAG 提交给执行内核。
- V1 使用 Python 3.12、Pydantic v2、SQLAlchemy 2.0 Core、Alembic 和 pytest。领域模型与状态机使用 dataclass/enum，Pydantic 用于边界 DTO、工作流定义和工具输入输出。
- 工作流核心按 Domain、Application、Ports、Adapters 分层。领域层不依赖 Web、SQLAlchemy 或 SQLite；后续 Agent、API、沙箱和控制台只能通过应用服务与稳定端口接入。
- 持久化采用“不可变工作流定义 + 可变运行状态”。SQLite 是首个适配器，启用 WAL、外键约束与 busy timeout；仓储和事务端口保留 PostgreSQL 适配边界。
- 运行、步骤、尝试、审批和审计事件分别持久化。每次状态转换与对应事件在同一短事务中提交，并用 revision 乐观锁处理审批与协调器并发写入。
- V1 使用单协调器和有界 ThreadPoolExecutor。协调器统一计算就绪步骤和提交状态，工作线程只返回 ExecutionOutcome，不直接写数据库。默认并发 4、硬上限 16。
- V1 仅支持静态 DAG，不支持条件路由、动态加步骤或通用表达式。单运行最多 100 个步骤、1000 条依赖边。
- 工具必须显式注册并声明名称、版本、Pydantic Schema、风险、幂等能力、超时和敏感字段。补偿作为独立注册工具显式关联，不使用隐藏 rollback 方法。
- 工具调用跨越两个事务：调用前原子写入 running、attempt 和开始事件；调用后原子写入结果、终态和结束事件。进程中断后使用稳定幂等键探测既有副作用。
- 错误分为 retryable、permanent、result_unknown 和内核一致性错误。默认最多 3 次尝试、硬上限 10；每次重试创建新 attempt，但复用逻辑幂等键。
- 任一高风险步骤等待审批时形成全局审批屏障：停止分派新步骤，等待在途工具结束。审批绑定工作流摘要、步骤、工具版本和脱敏参数摘要。
- 失败后，内核按成功步骤的逆拓扑顺序调度补偿；互不依赖步骤按持久化完成序号逆序。补偿失败进入 manual_intervention，并同时保留原始错误与补偿错误。
- 工具参数不保存原始密钥，只保存 SecretRef；审计、日志、异常与结果统一脱敏。V1 审计是应用层追加式记录，不宣称 SQLite 文件具备密码学防篡改能力。

## 关键取舍与风险

- 自研执行内核增加状态机、恢复和适配器工作量，但能清楚展示数据库事务、幂等、补偿和可靠性工程能力；LangGraph 留在规划层以覆盖主流 Agent 技术而不模糊执行边界。
- 同步 SQLAlchemy Core 让事务行为显式且便于 Windows/WSL2 故障测试，但 V1 不追求高吞吐；未来 PostgreSQL 与多协调器需要增加持久化租约。
- SQLite 只支持单协调器。V1 不实现分布式 Worker、消息队列、Docker、生产连接、多租户或复杂 RBAC。
- Python 工作线程不能可靠强杀超时函数。工具必须设置底层 I/O 截止时间；无法确认结果时停止自动执行并转人工介入。
- 系统采用至少一次调度语义，不承诺外部副作用恰好一次。非幂等工具结果未知时禁止自动重试。
- 补偿是业务恢复动作，不等同于全局事务回滚；无法安全补偿时必须暴露人工处理状态。
- V1 审计事件永久保留，不实现自动清理；后续通过导出和保留策略扩展。

## 测试策略

- 领域单元测试覆盖完整状态转换表、非法转换、DAG 校验、稳定就绪排序、审批绑定、错误分类和补偿顺序。
- 使用 Hypothesis 生成 DAG，验证依赖安全、单次有效副作用和逆依赖补偿等不变量。
- 内存与 SQLite 适配器运行同一套仓储契约测试，覆盖事务回滚、乐观锁、审批唯一性、事件序号及状态/事件原子提交。
- 协调器集成测试使用可编排 Fake Tool、Fake Clock 和确定性 ID，覆盖并行、审批、重试、结果未知、补偿成功与补偿失败。
- 进程恢复测试使用 SQLite 保存工作流状态、独立文件账本模拟外部副作用，并在调用前、调用后未提交、等待审批和补偿后未提交等故障点强制退出子进程。
- 核心验收使用固定订单服务升级 DAG 和 Fake Tool，验证正常成功、健康检查失败后补偿、工具成功后进程中断恢复三条路径；测试不依赖 DeepSeek、Docker 或 PostgreSQL，并支持 Windows 与 WSL2。
- 安全测试覆盖未注册工具、非法参数、目录/密钥泄露、异常文本脱敏和过期审批拒绝。

## Spec Patch

- 在 `specs/reliable-workflow-runtime/spec.md` 的“所有关键行为必须形成可查询审计轨迹”下增加场景：提交状态转换及对应审计事件发生持久化错误时，两者均不得部分提交。
- 在 OpenSpec 高层 `design.md` 中删除“V1 支持条件路由”的表述，使其与已确认的静态 DAG 范围和 canonical delta spec 保持一致。
- 应用以上变更后重新运行 OpenSpec 严格校验，并重新生成 Comet design handoff 以更新 source hash 与 handoff_hash。
