# ChangePilot

[English](README.md)

[在线文档](https://inevitable-HE.github.io/ChangePilot/)

第一次接触项目，建议先阅读[中文用户指南](docs/zh/user-guide.md)，理解应用场景、
操作者流程、可靠性模型和当前边界，再运行演示。

ChangePilot 是一个建立在可靠工作流运行时之上的受约束变更规划 Agent。
它把服务升级请求转化为带知识证据的有向无环任务图，应用确定性的风险与
补偿策略，在信息不足或高风险操作前暂停，并把通过校验的计划交给持久化
执行器。

参考场景会升级订单服务并执行数据库 Schema 迁移：

```text
提交请求 -> 检索操作手册 -> 生成计划 -> 校验并至多修复一次
        -> 准备工作流 -> 检查服务与数据库 -> 前置检查
        -> 人工审批 -> Schema 迁移 -> 服务部署 -> 验证
        -> 失败时反向补偿
```

这个项目被称为 Agent，不是因为简单使用了 RAG 或 LangGraph，而是因为它
形成了一个受约束的“感知、推理、行动”闭环：系统收集上下文，使用模型生成
面向目标的计划，通过工具契约和安全策略进行确定性校验，在信息不足时发起
澄清，并把真实副作用交给带审批、幂等、恢复、补偿和审计能力的运行时。

## 环境要求

- Python 3.12
- 运行运营控制台需要 Node.js 20+ 与 npm
- Windows、WSL 或 Linux
- 默认不需要 Docker
- 只有主动运行在线冒烟测试时才需要 DeepSeek API Key

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m alembic upgrade head
```

可选安装本地 BGE Embedding，以启用混合检索：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,retrieval]"
```

不安装该扩展时，SQLite FTS5 关键词检索仍然可用，所有默认测试均可离线
运行。

## 离线 Agent 演示

验收测试使用 `examples/runbooks` 中的示例操作手册、确定性的 Mock LLM 和
真实工作流运行时：

```powershell
.venv\Scripts\python.exe -m pytest tests\planning\acceptance\test_order_upgrade_planning.py -q
```

该测试验证 Agent 能够检索证据、生成七步计划、把 Schema 迁移提升为高风险
操作、将计划转换为工作流 DAG、执行只读前置检查，并在 `schema.migrate`
之前停下等待审批。整个过程不访问网络，也不会消耗 API 额度。

提示注入边界测试：

```powershell
.venv\Scripts\python.exe -m pytest tests\planning\security\test_prompt_injection.py -q
```

## 离线执行沙箱

执行沙箱承接已批准的计划，对隔离的 V1 订单服务和 SQLite 数据库产生真实
本地副作用。可以分别重放成功升级、健康检查失败后的反向补偿，以及 Schema
迁移已提交但结果尚未确认时的进程重启恢复：

```powershell
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario success --root .demo\success
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario compensation --root .demo\compensation
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario recovery --root .demo\recovery
```

每条命令都会输出最终工作流状态和沙箱状态的 JSON 摘要。这三个场景仅依赖
本地 Python 与 SQLite，不需要 DeepSeek、Docker、PostgreSQL 或网络连接。

## 运营 API

阶段 4 提供面向操作者的本地 API，覆盖结构化变更请求、计划审阅、批准或
拒绝、运行快照、带游标的审计事件、SSE 增量事件、故障恢复，以及由后端
生成的 Markdown/JSON 报告：

```powershell
$env:CHANGEPILOT_OPERATIONS_ROOT = ".operations"
.venv\Scripts\python.exe -m uvicorn changepilot.operations.server:app --reload
```

启动后访问 `http://127.0.0.1:8000/docs` 查看资源型 API。V1 只驱动隔离的
订单服务演示场景，不接受任意 SQL、Shell 命令、工具调用或生产环境路径。

## 运营控制台

保持 API 运行，并在另一个终端启动 React 控制台：

```powershell
cd console
npm install
npm run dev
```

访问 `http://127.0.0.1:5173`。控制台支持结构化变更提交、计划与知识证据
审阅，并会在执行前运行完整 Agent 规划链路。“Agent 规划轨迹”会展示
LangGraph 节点、RAG 引用、计划校验、知识快照、模型信息、调用次数和 Token
用量。操作者随后可以审阅工具绑定计划，批准或拒绝风险动作，跟踪实时审计
事件，恢复中断任务并导出报告。顶栏可切换中英文界面，语言选择会保存在
本地浏览器中。

`success`、`compensation`、`recovery` 用于演示写入型变更工作流；
`readiness` 使用另一种目标生成三步只读计划，不包含迁移、部署、审批或补偿。
所有场景都只作用于隔离的本地沙箱。

安装 Python 与前端依赖后，也可以用一条命令同时运行两项本地服务，按
`Ctrl+C` 统一停止：

```powershell
.\scripts\start-operations-demo.ps1
```

## 离线评测

版本化核心数据集包含开发集与保留集，覆盖规划、审批阻断、成功执行、补偿和
重启恢复。使用 Mock LLM 与本地 SQLite 沙箱运行：

```powershell
.venv\Scripts\python.exe -m changepilot.evaluation.cli `
  --dataset examples\evaluations\core-v1.json `
  --output .eval-results\core-v1
```

命令会生成可比较的 JSON 与 Markdown 报告；任一声明的安全阈值发生回归时
返回非零退出码。默认运行不会读取 DeepSeek Key，也不会访问网络。

仓库已在 `examples/evaluations/baseline-v1/evaluation.json` 固化首个通过
基线。将新结果与基线比较：

```powershell
.venv\Scripts\python.exe -m changepilot.evaluation.cli `
  --dataset examples\evaluations\core-v1.json `
  --output .eval-results\candidate `
  --baseline examples\evaluations\baseline-v1\evaluation.json
```

只有显式设置 `CHANGEPILOT_RUN_ONLINE_EVAL=1` 且提供 DeepSeek Key，在线评测
保护层才允许启动。下一样例或模型调用一旦会超过样例数、调用次数、Token 或
预估费用上限，系统会在发起调用前拒绝。日常开发默认且推荐使用离线评测。

```powershell
$env:CHANGEPILOT_RUN_ONLINE_EVAL = "1"
$env:CHANGEPILOT_ONLINE_EVAL_MAX_CASES = "1"
$env:CHANGEPILOT_ONLINE_EVAL_MAX_CALLS = "1"
$env:CHANGEPILOT_ONLINE_EVAL_MAX_TOKENS = "1024"
$env:CHANGEPILOT_ONLINE_EVAL_MAX_COST = "0.01"
$env:CHANGEPILOT_DEEPSEEK_API_KEY = "..."
```

## 操作手册更新

操作手册使用带 YAML Frontmatter 的 Markdown：

```yaml
---
document_id: schema-migration
version: "1.1"
source: internal/runbooks/schema-migration.md
trust_level: authoritative
policy_key: schema-migration
effective_at: "2026-07-24"
status: active
rule_digest: schema-migration-policy-v2
---
```

通过应用接口加载并写入一个版本：

```python
from changepilot.planning.adapters.knowledge.sqlite import (
    SQLiteKnowledgeStore,
    initialize_planning_schema,
)
from changepilot.planning.application.ingestion import (
    RunbookIngestionService,
    load_runbook,
)
from changepilot.workflow.adapters.persistence.sqlite import create_sqlite_engine

engine = create_sqlite_engine("workflow-runtime.db")
initialize_planning_schema(engine)
store = SQLiteKnowledgeStore(engine)
result = RunbookIngestionService(store).ingest(
    load_runbook("examples/runbooks/schema-migration.md")
)
print(result.snapshot.digest)
```

`document_id` 与 `version` 的组合不可变。内容发生变化时必须提升版本；重复
写入完全相同的内容是幂等操作。规划结果会记录知识快照摘要，如果生成计划后
知识库发生变化，系统将拒绝准备执行。

## DeepSeek 配置

模型供应商使用 OpenAI 兼容接口，配置从以下环境变量读取：

```powershell
$env:CHANGEPILOT_DEEPSEEK_API_KEY = "..."
$env:CHANGEPILOT_LLM_BASE_URL = "https://api.deepseek.com"
$env:CHANGEPILOT_LLM_MODEL = "deepseek-v4-flash"
$env:CHANGEPILOT_LLM_TIMEOUT_SECONDS = "30"
```

运营 API 默认使用离线确定性规划模型，但请求仍会经过真实的 RAG、LangGraph、
结构化生成、计划校验和工作流映射链路。如需明确让 DeepSeek 生成控制台计划：

```powershell
$env:CHANGEPILOT_PLANNER_MODE = "deepseek"
```

`BudgetedModelGateway` 会限制调用次数、Token 和可选的预估费用，对相同请求
使用缓存，并只允许有限次数重试。在线冒烟测试默认跳过。需要主动花费一次
小额请求时执行：

```powershell
$env:CHANGEPILOT_RUN_LIVE_LLM = "1"
.venv\Scripts\python.exe -m pytest tests\planning\live\test_deepseek_smoke.py -q
```

## 验证

```powershell
.venv\Scripts\python.exe -m pytest tests\planning -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\sandbox -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\operations -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\evaluation -q -p no:cacheprovider
npm --prefix console run build
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest --cov=changepilot --cov-report=term-missing --cov-fail-under=85 -q -p no:cacheprovider
openspec validate add-operations-console-and-evals --strict --json --no-interactive
git diff --check
```

架构说明：

- `docs/zh/architecture/agent-planning-and-knowledge.md`：Agent 边界与知识检索。
- `docs/zh/architecture/change-execution-sandbox.md`：本地执行与恢复场景。
- `docs/zh/architecture/reliable-workflow-core.md`：事务、审批、恢复、补偿与审计。
- `docs/zh/architecture/operations-console-and-evaluation.md`：运营控制台与评测边界。

## 参与开发

开发环境、分支规范、仓库边界和 Pull Request 要求见 `CONTRIBUTING.md`。
