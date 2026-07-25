---
comet_change: add-agent-planning-and-knowledge
role: technical-design
canonical_spec: openspec
archived-with: 2026-07-25-add-agent-planning-and-knowledge
status: final
---

# Agent 规划与知识检索技术设计

## 1. 设计目标

本阶段在自然语言研发变更与可靠工作流内核之间增加一个受约束的规划层。规划层负责澄清上下文、检索 Runbook、生成带证据的结构化计划，并在计划进入第一阶段运行时前执行确定性校验。

规划层是 Agent 决策系统，不是新的执行引擎。LangGraph 只编排无副作用的规划节点；事务执行、审批屏障、故障恢复和补偿继续由第一阶段工作流内核负责。

## 2. 核心原则

1. LLM 只能提出计划，不能直接执行工具。
2. 模型输出必须先通过 Pydantic Schema，再通过确定性策略。
3. Runbook 是带来源的证据数据，不是系统指令。
4. 工具目录和规划策略是风险等级、审批和补偿要求的事实源。
5. 默认开发和测试离线运行，真实 DeepSeek 调用显式启用并受预算保护。
6. 规划记录与运行记录通过工作流定义身份和摘要关联，不修改第一阶段核心持久化模型。

## 3. 模块边界

新增 `changepilot.planning` 包，并沿用现有的 domain、application、ports、adapters 分层：

```text
planning/
  domain/
    models.py          # 请求、证据、计划、结果和状态
    policies.py        # 工具规划策略、风险和证据规则
    failures.py        # 结构化规划错误
  ports/
    models.py          # ModelGateway
    knowledge.py       # KnowledgeStore、Retriever、EmbeddingProvider
    persistence.py     # PlanningRepository、UsageRepository、CacheRepository
  application/
    graph.py           # LangGraph StateGraph 装配和条件边
    nodes.py           # 规范化、检索、规划、校验、修复节点
    validation.py      # 确定性安全校验
    mapping.py         # ChangePlan -> WorkflowDefinition payload
    services.py        # 摄取、规划、澄清续接和准备提交
  adapters/
    models/
      mock.py
      deepseek.py
    knowledge/
      sqlite.py
      bge.py
```

领域层和应用层不得依赖 DeepSeek SDK、Sentence Transformers 或具体 SQLite 查询类型。LangGraph 节点通过端口接收依赖，使 Mock LLM、假向量和内存仓储可以覆盖默认测试。

## 4. 规划领域模型

所有外部边界使用冻结、`extra="forbid"` 的 Pydantic 模型。

主要输入：

- `ChangeRequest`：服务标识、当前版本、目标版本、变更内容、成功条件和用户约束。
- `ClarificationAnswer`：规划会话 ID、问题 ID 和用户补充内容。

主要证据：

- `DocumentIdentity`：文档 ID、版本、来源、可信级别和内容摘要。
- `EvidenceRef`：文档身份、片段 ID、片段位置和片段摘要。
- `RetrievedEvidence`：引用、展示文本、关键词排名、向量排名、混合得分和冲突标记。

主要计划：

- `PlanStep`：步骤 ID、工具引用、参数、依赖、风险、审批要求、成功条件、补偿意图和证据引用。
- `ChangePlan`：计划 ID、版本、目标、假设、步骤、知识快照摘要、Prompt 版本和内容摘要。
- `PreparedWorkflow`：计划身份、工作流字典和由 `WorkflowDefinition.from_mapping()` 计算的定义摘要。

规划终态采用可辨识联合类型：

- `clarification_required`
- `plan_ready`
- `planning_rejected`
- `budget_exhausted`

任何终态都不包含自动执行工具的入口。

## 5. LangGraph 有限状态图

图状态使用 `TypedDict`，对外输入、模型响应和最终输出仍使用 Pydantic。图不使用开放式 ReAct 循环。

```text
START
  -> normalize_request
  -> check_required_context
      -> clarification_required -> END
      -> retrieve_knowledge
  -> detect_conflicts
      -> clarification_required -> END
      -> generate_plan
  -> validate_plan
      -> plan_ready -> END
      -> repair_plan -> validate_repaired_plan
      -> planning_rejected -> END
```

修复计数硬限制为一次。只有 JSON、Schema 或可定位的普通结构错误允许进入修复节点。未知工具、越权参数、风险降级、无可信证据、循环依赖和缺失必要补偿属于策略错误，直接拒绝。

LangGraph 不启用独立持久化 checkpointer。规划会话、澄清记录、计划版本和调用记录由项目自己的 repository 保存，避免与第一阶段持久化职责重叠，也避免把领域状态绑定到框架内部格式。

## 6. 模型网关

`ModelGateway` 只暴露结构化请求和响应：

```text
generate(ModelRequest) -> ModelResponse
```

`ModelRequest` 包含模型配置、Prompt 版本、消息边界、目标 Schema、最大 token 和调用元数据。`ModelResponse` 包含原始响应摘要、解析后的 JSON、模型名、延迟、token 用量和结束原因。

DeepSeek 适配器使用 OpenAI-compatible Chat Completions：

- API Key 仅从环境变量读取。
- Base URL、模型名、超时和最大 token 均配置化。
- 默认模型为 `deepseek-v4-flash`，不把模型名散落在业务代码中。
- 使用 JSON Output；Pydantic 仍是 Schema 约束的事实源。
- 仅对超时、429 和 5xx 执行有限重试。
- 日志不保存 API Key、完整敏感 Prompt 或模型思维链。

`MockModelGateway` 支持按调用顺序编排成功、澄清、格式错误、瞬时错误和预算耗尽响应。

## 7. 预算、缓存与观测

`BudgetedModelGateway` 装饰具体网关，在发起调用前检查：

- 单次规划最大调用数
- 最大输入和输出 token
- 配置化费用上限
- 规划会话累计用量

缓存键由模型名、模型参数、Prompt 版本、Schema 版本、规范化请求摘要和知识快照摘要组成。缓存命中记录零新增费用，但保留命中事件。

调用记录只保存请求摘要、模型、延迟、token、估算费用、重试次数、缓存状态和错误分类。价格通过配置提供，避免价格变化要求修改业务代码。

## 8. Runbook 摄取与版本

V1 支持 Markdown 和纯文本。摄取流程：

1. 规范化来源和元数据。
2. 计算文档内容摘要。
3. 相同文档 ID、版本和摘要直接复用。
4. 按 Markdown 标题和大小边界进行稳定分块。
5. 为片段生成包含文档身份、顺序和内容摘要的稳定 ID。
6. 更新 FTS5 索引，并在启用嵌入时增量生成向量。

文档元数据至少包含：

- `document_id`
- `version`
- `source`
- `trust_level`
- `content_digest`
- `policy_key`
- `effective_at`
- `status`

同一 `policy_key` 下存在多个有效且规则摘要不同的文档时，检索结果携带冲突标记。规划流程不得静默选择宽松规则。

## 9. 混合检索

默认关键词检索使用 SQLite FTS5/BM25。语义检索通过 `EmbeddingProvider` 端口提供：

- 默认离线测试使用确定性假向量。
- 可选真实适配器使用 `BAAI/bge-small-zh-v1.5`。
- 模型依赖使用可选安装项和延迟导入，不阻塞默认安装。

向量以模型 ID、维度和二进制值保存。V1 面向小规模 Runbook，在进程内执行精确余弦检索，不引入独立向量数据库。关键词结果和向量结果使用加权 Reciprocal Rank Fusion 合并。

每个结果必须返回稳定证据引用。低于阈值时返回 `insufficient_evidence`，不得让模型补写不存在的依据。

## 10. 不可信内容隔离

系统规则、用户请求、工具元数据和 Runbook 证据分别构造消息段。证据以带 ID 和来源的结构化数据传递，不拼接为系统指令。

确定性策略保证：

- Runbook 不能改变系统角色、模型配置或预算。
- Runbook 不能访问密钥或直接触发工具。
- 文档声明不能降低工具目录或规划策略给出的风险等级。
- 只有不可信证据时，高风险步骤保持阻塞。
- 冲突证据触发澄清或采用更严格规则。

Prompt 注入检测用于标记和审计，不作为唯一安全边界。

## 11. 计划确定性校验

校验按固定顺序执行并返回可定位错误：

1. Pydantic Schema 与大小限制。
2. 工具名称、版本和参数 Schema。
3. 步骤 ID、依赖目标、边数和 DAG 环检测。
4. 风险等级不得低于 `ToolDescriptor` 和 `PlanningToolPolicy`。
5. 高风险步骤必须要求审批。
6. 策略要求补偿的步骤必须提供已注册补偿工具。
7. 领域步骤引用的证据必须存在于当前知识快照。
8. 高风险步骤必须具有满足可信级别要求的证据。
9. 冲突或过期证据必须显式解决。
10. 映射结果必须通过现有 `WorkflowDefinition.from_mapping()`。

`PlanningToolPolicy` 单独描述工具是否有副作用、是否强制补偿以及最低证据级别。它补充现有 `ToolDescriptor`，不修改第一阶段工具执行契约。

## 12. 与第一阶段集成

`WorkflowDefinitionMapper` 只映射第一阶段支持的字段：

- 步骤 ID
- 工具名和版本
- 参数
- 依赖
- 最终风险等级
- 重试策略
- 补偿工具

成功条件、证据和审批依据保留在规划记录中。映射后调用 `WorkflowDefinition.from_mapping()`，生成规范化定义与摘要。

`PlanningService.prepare_workflow()` 返回 `PreparedWorkflow`，不调用 `WorkflowService.create_run()`。后续显式提交时必须重新检查：

- 计划仍为最新版本
- 知识快照未失效
- 工具目录和策略版本未变化
- 用户已完成必要确认

运行记录中的定义身份和摘要可以反查规划记录，实现审计关联。

## 13. SQLite 持久化

在现有 Alembic 链上增加规划迁移，包含：

- `knowledge_documents`
- `knowledge_chunks`
- `knowledge_chunk_embeddings`
- `knowledge_fts` 虚拟表
- `planning_sessions`
- `planning_attempts`
- `planning_plans`
- `model_usage`
- `model_cache`

仓储遵循现有 Unit of Work 风格。摄取新知识版本、保存计划版本和记录模型用量分别保持事务原子性。

## 14. 测试策略

开发期间只运行当前模块的定向测试；阶段末统一运行全量测试和覆盖率。

单元测试覆盖：

- 文档版本、稳定分块和引用
- BM25、向量和 RRF 排序
- 预算、缓存、重试和脱敏
- 缺失字段检测
- 计划 Schema、风险、审批、证据、补偿和 DAG 校验
- 工作流映射

集成测试覆盖：

- SQLite 摄取、增量更新和冲突标记
- LangGraph 澄清、成功、一次修复、拒绝和预算耗尽路径
- Prompt 注入与不可信证据
- 知识版本变化导致旧计划失效

端到端验收使用订单服务升级场景：

```text
自然语言目标
  -> 检索升级与迁移 Runbook
  -> 生成带引用计划
  -> 映射并验证 WorkflowDefinition
  -> 测试代码显式提交给 WorkflowService
  -> 第一阶段在数据库迁移前建立审批屏障
```

真实 DeepSeek 测试默认跳过，仅在显式环境开关、API Key 和严格调用/token 上限同时存在时运行。

## 15. 延后范围

- 通用互联网搜索
- 多 Agent 协作
- 自主工具执行
- LangSmith 托管部署
- 独立向量数据库
- 大规模离线模型评测
- Web 控制台和人工审批界面

这些能力分别属于后续执行沙箱和运维控制台阶段。
