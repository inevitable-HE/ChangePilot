## Why

可靠工作流内核只能执行已经结构化的任务图，但真实研发变更通常以自然语言目标、分散的操作规范和不完整的上下文开始。ChangePilot 需要一个受约束的规划 Agent，将用户意图和检索证据转换为可校验的变更计划，同时在信息不足或风险过高时主动停下来询问。

## What Changes

- 建立兼容 OpenAI API 的模型网关，接入 DeepSeek，并提供不消耗额度的 Mock LLM。
- 建立 Runbook 文档摄取、索引、检索和证据引用能力。
- 建立面向研发变更的需求澄清、风险识别和结构化计划生成流程。
- 将 Agent 输出约束为版本化 Schema，并在提交工作流内核前执行确定性校验。
- 建立模型调用预算、超时、重试、缓存和使用量记录。
- 建立 Prompt 注入与不可信知识内容的基本隔离规则。

## Capabilities

### New Capabilities

- `runbook-knowledge-retrieval`: 定义 Runbook 摄取、检索、证据引用和不可信内容处理要求。
- `change-planning-agent`: 定义需求澄清、模型提供方、结构化计划、风险标注、验证和成本控制要求。

### Modified Capabilities

无。

## Impact

- 新增模型网关、DeepSeek 与 Mock LLM 适配器、Prompt 模板和结构化输出模型。
- 新增本地知识库索引、检索服务和 Runbook 示例数据。
- 新增将规划结果提交到可靠工作流内核的适配层。
- DeepSeek API 密钥仅通过环境变量提供，不进入仓库、日志或审计事件。
- 本 change 不执行服务部署、数据库迁移或其他有副作用的真实工具。
