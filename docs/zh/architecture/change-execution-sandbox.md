# 变更执行沙箱

## 目标

执行沙箱把 ChangePilot 已校验的订单服务升级计划转化为可观测的本地副作用。它演示
企业变更 Agent 中连接计划生成与生产适配器的部分：

```text
已校验计划
  -> 检查服务与 Schema
  -> 执行兼容性前置检查
  -> 等待高风险审批
  -> 迁移 Schema
  -> 部署服务 V2
  -> 验证健康状态与订单行为
  -> 验证失败时按逆序补偿
```

这不只是一个提示词演示。模型可以协助生成计划，但授权、工具参数、副作用、幂等、
恢复、补偿和审计历史均由确定性代码控制。

## 边界

`changepilot.sandbox` 包含四层：

- `domain` 定义严格的工具参数、输出、版本、状态和故障。
- `application` 管理隔离环境、订单服务外观、确定性故障注入和运行时装配。
- `adapters` 实现 SQLite 状态和版本化工作流工具。
- `demo` 提供三个可重复运行的命令行场景。

每个沙箱由受限标识符寻址，并存放在配置的根目录下。工具只接收 `sandbox_id`，
不允许接收文件系统路径、SQL 语句、Shell 命令或网络地址。资源解析会拒绝绝对路径
以及越出所选沙箱的目录穿越。重置操作只删除已知状态文件。

本地沙箱结构如下：

```text
<root>/
  workflow.db
  sandboxes/order-demo/
    orders.db
    sandbox.json
    faults.json
```

`workflow.db` 保存持久化工作流检查点与审计事件；`orders.db` 保存 V1/V2 订单
Schema、迁移台账和工具副作用台账；`sandbox.json` 记录当前部署的服务版本；
`faults.json` 跨进程重启保存故障规则和调用计数。

## 固定升级流程

可执行工作流包含七个步骤：

| 步骤 | 工具 | 作用 |
| --- | --- | --- |
| `inspect-service` | `service.inspect` | 读取已部署服务版本 |
| `inspect-db` | `schema.inspect` | 读取 Schema 版本和指纹 |
| `precheck` | `upgrade.precheck` | 校验 V1 基线兼容性 |
| `migrate-schema` | `schema.migrate` | 执行高风险 V1 到 V2 迁移 |
| `deploy-v2` | `service.deploy-v2` | 在 Schema 迁移后切换服务 |
| `health-check` | `service.health-check` | 验证服务与数据库兼容性 |
| `smoke-test` | `service.smoke-test` | 测试版本化订单契约 |

迁移步骤需要审批和预期数据库指纹。迁移与部署工具会持久化逻辑幂等键。重复调用已确认
的操作时会返回之前的副作用结果，不会再次执行。

补偿元数据将 `service.deploy-v2` 映射到 `service.restore`，将
`schema.migrate` 映射到 `schema.rollback`。工作流按完成顺序的逆序补偿。
Schema 回滚还会拒绝丢弃只存在于 V2 的业务数据，因此补偿受到显式安全策略约束。

## 恢复语义

故障注入区分执行中断发生的位置：

- `before_effect`：外部状态尚未变化。恢复探测返回 `NOT_FOUND`，随后使用相同逻辑
  幂等键重试。
- `after_effect`：状态已经变化，但结果未被工作流确认。恢复探测返回 `FOUND`，
  将副作用记录为已完成，不再重复执行。
- `permanent_failure`：工具返回不可重试错误，工作流开始补偿已经完成的可逆步骤。

恢复会同时使用持久化工作流检查点和真实沙箱状态，避免进程重启后只相信部分完成操作
的某一侧状态。

## 重放演示

所有场景只依赖本地 Python 和 SQLite：

```powershell
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario success --root .demo\success
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario compensation --root .demo\compensation
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario recovery --root .demo\recovery
```

JSON 结果会展示工作流终态、服务与 Schema 版本、迁移台账和审计事件数量。恢复场景
还会证明已提交迁移只被调用一次。

这些场景不需要 DeepSeek API Key、Docker Daemon、PostgreSQL 服务或网络连接。
SQLite 作为参考适配器，使执行语义能够以较低成本接受审阅和测试。

## 生产适配器演进路径

PostgreSQL 或 Docker 实现可以替换沙箱数据库和服务适配器，同时保留相同的版本化
工具契约、风险策略、幂等键、探测、补偿元数据和工作流事件。生产集成还需要：

- 密钥管理和工作负载身份；
- 真实部署与数据库迁移 API；
- 分布式锁和更强的并发控制；
- 组织级审批与授权；
- 备份、时间点恢复、可观测性和保留策略。

本地适配器不宣称具备生产级隔离或分布式事务保证。它的目标是在连接高权限基础设施前，
让 Agent 的执行契约、失败行为和恢复决策变得具体、可验证。
