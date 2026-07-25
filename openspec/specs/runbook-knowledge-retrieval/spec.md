# runbook-knowledge-retrieval Specification

## Purpose
TBD - created by archiving change add-agent-planning-and-knowledge. Update Purpose after archive.
## Requirements
### Requirement: Runbook 必须以可追踪版本摄取
系统 MUST 为每份 Runbook 保存稳定文档标识、版本、来源、内容摘要和分块位置，并在内容发生变化时生成新的知识版本。

#### Scenario: 摄取新版本文档
- **WHEN** 已存在 Runbook 的内容摘要发生变化
- **THEN** 系统保存新版本并使后续检索结果能够区分新旧来源

#### Scenario: 重复摄取相同文档
- **WHEN** 文档标识、版本和内容摘要均未变化
- **THEN** 系统复用已有索引且不创建重复分块

### Requirement: 检索结果必须包含稳定证据引用
系统 SHALL 根据变更目标检索相关 Runbook 片段，每个片段 MUST 包含文档标识、版本、位置、相关度信息和可展示文本。

#### Scenario: 检索数据库迁移规范
- **WHEN** 规划请求包含数据库 Schema 迁移
- **THEN** 检索结果返回相关迁移规范片段及其稳定来源引用

#### Scenario: 没有足够相关证据
- **WHEN** 最高相关结果低于配置阈值
- **THEN** 系统明确返回证据不足状态，而不是提供无来源内容

### Requirement: 规划结论必须可追溯到检索证据
系统 MUST 允许计划步骤和风险判断引用一个或多个检索片段，并拒绝指向不存在知识版本的引用。

#### Scenario: 查看计划证据
- **WHEN** 用户检查一个由 Runbook 支撑的计划步骤
- **THEN** 系统返回该步骤引用的文档、版本和原始片段位置

### Requirement: 检索内容必须作为不可信数据隔离
系统 MUST 将 Runbook 文本与系统指令、用户输入和工具元数据隔离。检索内容不得改变系统角色、访问密钥、绕过审批或直接触发工具执行。

#### Scenario: Runbook 包含提示注入文本
- **WHEN** 检索片段要求忽略系统规则并自动执行高风险工具
- **THEN** 系统仅将该文本作为文档内容传递，且确定性风险与审批策略仍然生效

#### Scenario: 高风险步骤只有不可信证据
- **WHEN** 高风险计划步骤仅引用可信级别不足的 Runbook 片段
- **THEN** 系统不得用这些片段满足证据要求，并将计划保持为阻塞状态

### Requirement: 冲突知识必须暴露给规划流程
系统 SHALL 检测相同主题下版本或规则明显冲突的检索结果，并向规划流程提供冲突标记和各自来源。

#### Scenario: 两份迁移规范的审批要求冲突
- **WHEN** 检索到的有效文档对同一操作给出不兼容要求
- **THEN** 系统返回冲突证据，规划流程要求用户确认或采用更严格约束

