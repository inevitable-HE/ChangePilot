# Comet Design Handoff

- Change: establish-reliable-workflow-core
- Phase: design
- Mode: compact
- Context hash: 828de7d9f3d0eec0d75c698dca14d8fa4972e31a028d2f80e1c3c8d33b354dac

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## openspec/changes/establish-reliable-workflow-core/proposal.md

- Source: openspec/changes/establish-reliable-workflow-core/proposal.md
- Lines: 1-28
- SHA256: e9e0a67ffa6fe85c43f98cf75928d059c30fc1164dbc13547fa5a83a2adbaca1

```md
## Why

研发变更通常跨越多个有依赖关系的步骤，执行过程中还会遇到人工审批、外部工具失败和进程中断。ChangePilot 需要一个与大模型解耦的可靠工作流内核，确保任务能够按依赖顺序执行，并在异常后安全恢复，而不是依赖一次不可追踪的 Agent 对话。

## What Changes

- 建立基于有向无环图的变更任务模型，显式描述步骤、依赖和执行状态。
- 建立持久化运行状态与检查点机制，支持暂停、恢复和进程重启后的续执行。
- 建立人工审批节点，使高风险步骤在获得明确批准前不可执行。
- 建立幂等键、重试策略和错误分类，避免恢复或重试时重复执行已成功步骤。
- 建立统一工具契约和事件审计模型，为后续 Agent 规划、变更工具和控制台提供稳定接口。

## Capabilities

### New Capabilities

- `reliable-workflow-runtime`: 定义任务图执行、状态持久化、审批、幂等、重试、恢复和审计事件的行为要求。

### Modified Capabilities

无。

## Impact

- 新增 ChangePilot 后端的工作流领域模型、持久化接口、执行器和工具契约。
- 新增运行、步骤、审批和事件等结构化数据模型及内部服务接口。
- 后续 Agent 规划、沙箱执行和 Web 控制台均依赖该能力。
- 本 change 不接入真实大模型、订单服务、数据库迁移工具或生产环境。

```

## openspec/changes/establish-reliable-workflow-core/design.md

- Source: openspec/changes/establish-reliable-workflow-core/design.md
- Lines: 1-72
- SHA256: eb6d8beca4406e14da5b9d55efabdef3262c38dd17866031fed8a1ae0675d9b8

```md
## Context

ChangePilot 后续需要同时承载大模型生成的计划、人工审批以及外部工具执行。大模型输出具有不确定性，外部工具也可能超时、部分成功或在返回结果前中断，因此执行可靠性不能由 Prompt 或 Agent 对话历史保证。

首个版本面向单机开发与演示，目标环境为 WSL 2 Ubuntu，同时需要保持在 Windows 原生环境执行单元测试的可能性。系统暂不依赖 Docker、消息队列或外部工作流服务，但数据模型和持久化接口应允许后续迁移到 PostgreSQL。

## Goals / Non-Goals

**Goals:**

- 以确定性状态机执行有向无环任务图，并验证依赖合法性。
- 在同一持久化事务中提交步骤状态与审计事件。
- 支持人工审批、进程中断恢复、有限重试和幂等执行。
- 通过稳定的工具契约隔离工作流内核与具体业务工具。
- 为后续 Agent、API、控制台和评测提供可查询的运行视图。

**Non-Goals:**

- 不在工作流内核中实现 LLM 推理或 RAG。
- 不支持任意循环图、分布式调度、多租户和复杂 RBAC。
- 不宣称跨多个外部系统提供严格 ACID 事务。
- 不直接操作生产环境。

## Decisions

### 1. 采用确定性工作流内核，LLM 只提交结构化计划

工作流定义由步骤和依赖边组成，创建运行前必须完成 Schema 校验、环检测和工具注册检查。内核只接受通过验证的结构化定义，不从自然语言动态解释执行语义。

备选方案是让 Agent 在 ReAct 循环中自行决定下一工具。该方式实现简单，但难以稳定恢复、审计和阻止重复副作用，因此不作为可靠执行路径。

### 2. 区分不可变工作流定义与可变运行状态

工作流定义在运行开始后保持不可变，并记录定义版本和内容摘要。运行状态分别记录工作流、步骤尝试、审批请求和事件，避免恢复时重新解释已经开始执行的计划。

### 3. 采用显式状态机和追加式事件审计

运行状态至少包含 `pending`、`running`、`waiting_approval`、`succeeded`、`failed`、`compensating`、`compensated` 和 `cancelled`。每次合法转换都产生追加式事件；当前状态用于快速查询，事件流用于审计和恢复分析。

### 4. 使用持久化端口隔离存储实现

领域层通过仓储和事务接口访问存储。第一版使用 SQLite 适配器以降低 Windows/WSL 开发门槛，并通过迁移工具维护 Schema；后续可增加 PostgreSQL 适配器而不改变领域契约。

### 5. 采用至少一次调度语义与工具幂等契约

进程可能在工具产生副作用后、结果持久化前退出，因此系统不承诺恰好一次执行。每个副作用步骤必须携带稳定的幂等键，工具需要支持按幂等键查询或复用结果；不具备幂等能力的工具必须标记为需要人工恢复。

### 6. 将审批作为持久化阻塞状态

风险策略决定步骤是否需要审批。进入审批状态时保存工具名称、参数摘要、风险原因和计划版本；只有与当前审批请求匹配的批准结果才能恢复运行，修改参数后必须重新审批。

### 7. 补偿是显式步骤，不等同于数据库回滚

外部副作用通过与正向步骤关联的补偿步骤恢复。补偿按已成功步骤的逆依赖顺序执行，并独立记录结果；补偿失败会进入人工介入状态，不掩盖原始错误。

## Risks / Trade-offs

- [SQLite 并发能力有限] → V1 限制为单机、低并发执行，并保留 PostgreSQL 适配边界。
- [工具无法保证幂等] → 工具声明能力等级，高风险非幂等工具强制审批并提供人工恢复状态。
- [状态与外部副作用不可能形成全局事务] → 使用幂等键、结果探测和补偿步骤处理不确定结果。
- [工作流定义过度灵活会扩大实现范围] → V1 只支持静态无环图和有限重试，不支持条件路由、动态添加步骤或任意循环 DSL。
- [事件日志可能包含敏感参数] → 默认记录参数摘要和脱敏结果，不记录密钥及完整敏感载荷。

## Migration Plan

这是新建能力，无存量数据迁移。实现顺序为领域模型与状态转换、内存适配器、SQLite 持久化、执行器、审批和恢复测试。若后续引入 PostgreSQL，通过新增适配器和数据迁移脚本完成，不改变上层工作流契约。

## Open Questions

- 深度设计阶段确定采用自研轻量调度器，还是复用 LangGraph 的部分持久化能力。
- 确定 SQLite ORM/迁移组件以及 PostgreSQL 兼容策略。
- 明确单个运行允许的最大步骤数、重试次数和事件保留策略。

```

## openspec/changes/establish-reliable-workflow-core/tasks.md

- Source: openspec/changes/establish-reliable-workflow-core/tasks.md
- Lines: 1-31
- SHA256: 04352b927f6003c4d31c463ad39a1964e14a7654fc158d96fb40b623d749ce7f

```md
## 1. 项目与领域模型

- [ ] 1.1 建立后端项目结构、依赖管理、配置加载和测试基线
- [ ] 1.2 定义工作流、步骤、依赖、工具调用、审批和审计事件领域模型
- [ ] 1.3 实现工作流定义 Schema 校验、工具注册检查和任务图环检测

## 2. 状态机与持久化

- [ ] 2.1 实现工作流与步骤的合法状态转换及单元测试
- [ ] 2.2 定义仓储、事务和时钟等基础端口，并提供内存测试适配器
- [ ] 2.3 实现 SQLite 数据模型、迁移脚本和持久化适配器
- [ ] 2.4 保证状态转换与审计事件在同一持久化事务中提交

## 3. 调度与工具执行

- [ ] 3.1 实现基于依赖关系的就绪步骤计算和确定性调度器
- [ ] 3.2 实现工具注册表、参数校验、结果模型和敏感字段脱敏
- [ ] 3.3 实现稳定幂等键、步骤尝试记录和既有结果探测接口
- [ ] 3.4 实现错误分类、有限重试、退避和结果未知处理

## 4. 审批、恢复与补偿

- [ ] 4.1 实现风险策略与持久化审批请求、批准、拒绝和失效逻辑
- [ ] 4.2 实现进程重启后的运行扫描、检查点恢复和续执行
- [ ] 4.3 实现补偿步骤关联、逆依赖执行和人工介入状态

## 5. 接口与验证

- [ ] 5.1 提供创建运行、查询状态、提交审批和读取时间线的内部服务接口
- [ ] 5.2 编写正常执行、非法任务图、审批暂停、重试、崩溃恢复、幂等和补偿失败测试
- [ ] 5.3 补充架构说明、状态机图和本地运行文档

```

## openspec/changes/establish-reliable-workflow-core/specs/reliable-workflow-runtime/spec.md

- Source: openspec/changes/establish-reliable-workflow-core/specs/reliable-workflow-runtime/spec.md
- Lines: 1-104
- SHA256: d0c357d4fd84e8e1956bba020e5f3987e07651bf8f2bf5cf636a59ed464e483b

[TRUNCATED]

```md
## ADDED Requirements

### Requirement: 工作流定义必须通过结构校验
系统 MUST 在创建运行前验证步骤标识唯一、依赖目标存在、任务图无环、工具已注册且参数符合工具 Schema。校验失败的定义不得进入可执行状态。

#### Scenario: 拒绝存在环的任务图
- **WHEN** 工作流定义包含一条使任务图形成环的依赖边
- **THEN** 系统返回可定位到相关步骤的校验错误，且不创建工作流运行

#### Scenario: 接受合法任务图
- **WHEN** 工作流定义中的步骤、依赖和工具参数全部合法
- **THEN** 系统创建带有定义版本与内容摘要的工作流运行

### Requirement: 步骤必须按依赖关系调度
系统 SHALL 仅调度所有前置依赖均已成功的步骤，并允许互不依赖的就绪步骤按照执行器容量运行。

#### Scenario: 前置步骤未完成
- **WHEN** 某步骤仍存在未成功的前置依赖
- **THEN** 系统保持该步骤为等待状态且不调用其工具

#### Scenario: 多个步骤同时就绪
- **WHEN** 多个步骤的前置依赖均已成功且彼此无依赖关系
- **THEN** 系统将这些步骤标记为可调度，而不引入额外的顺序约束

### Requirement: 状态转换必须确定且可验证
系统 MUST 使用显式状态机管理工作流和步骤状态，只允许预定义的状态转换，并拒绝非法或过期的转换请求。

#### Scenario: 拒绝成功步骤再次进入运行态
- **WHEN** 调用方尝试将已成功步骤转换回运行状态
- **THEN** 系统拒绝转换并保留原状态，同时记录非法转换事件

### Requirement: 运行状态必须持久化并可恢复
系统 MUST 持久化工作流定义版本、当前运行状态、步骤尝试、待处理审批和审计事件，使新进程能够从最近一次已提交状态恢复。

#### Scenario: 进程在步骤之间退出
- **WHEN** 进程在一个步骤成功提交后、下一个步骤开始前退出并重新启动
- **THEN** 系统从已提交检查点恢复，且不会重新执行已成功步骤

#### Scenario: 存在待审批请求时重启
- **WHEN** 工作流处于等待审批状态并发生进程重启
- **THEN** 系统恢复相同审批请求，且在收到有效决定前不执行受保护步骤

### Requirement: 高风险工具必须经过人工审批
系统 SHALL 根据风险策略在工具执行前创建持久化审批请求。审批请求 MUST 包含工具、参数摘要、风险原因和工作流定义版本，批准、拒绝和修改操作均必须产生审计事件。

#### Scenario: 未批准的高风险步骤
- **WHEN** 高风险步骤已就绪但尚未获得有效批准
- **THEN** 系统暂停该工作流并且不调用对应工具

#### Scenario: 参数在批准后发生变化
- **WHEN** 已批准步骤的工具参数或工作流定义版本发生变化
- **THEN** 原批准失效，系统创建新的审批请求并继续保持暂停

#### Scenario: 审批被拒绝
- **WHEN** 审批人拒绝高风险步骤并提供原因
- **THEN** 系统不执行工具，将运行转入相应终止或待修订状态，并记录拒绝原因

### Requirement: 副作用工具必须支持幂等恢复
系统 MUST 为每次逻辑步骤生成稳定幂等键，并将其传递给声明支持幂等的工具。在恢复和重试时，系统 SHALL 使用同一幂等键查询或复用既有结果。

#### Scenario: 工具成功后进程在持久化前退出
- **WHEN** 工具已经完成副作用但调用进程在保存成功结果前退出
- **THEN** 恢复后的执行使用相同幂等键探测既有结果，不重复产生副作用

#### Scenario: 工具不支持幂等探测
- **WHEN** 非幂等工具返回结果未知或在调用期间失联
- **THEN** 系统停止自动重试并进入需要人工介入的状态

### Requirement: 重试必须受错误分类和策略约束
系统 SHALL 区分可重试、不可重试和结果未知的错误。自动重试 MUST 遵守最大次数与退避策略，且每次尝试都必须独立记录。

#### Scenario: 短暂网络错误
- **WHEN** 幂等工具返回可重试的短暂网络错误且未超过重试上限
- **THEN** 系统按照退避策略创建下一次尝试并保留同一逻辑步骤标识

#### Scenario: 参数校验错误
- **WHEN** 工具返回不可重试的参数校验错误
- **THEN** 系统立即停止自动重试并将步骤标记为失败

### Requirement: 失败恢复必须使用显式补偿步骤

```

Full source: openspec/changes/establish-reliable-workflow-core/specs/reliable-workflow-runtime/spec.md
