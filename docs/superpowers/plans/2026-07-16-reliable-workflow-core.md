---
change: establish-reliable-workflow-core
design-doc: docs/superpowers/specs/2026-07-16-reliable-workflow-core-design.md
base-ref: 64eaa1c323836383d78de1310881a446083957d0
---

# ChangePilot 可靠工作流内核实施计划

> **供 Agent 实施者使用：** 必须加载 `subagent-driven-development`（推荐）或 `executing-plans`，逐任务执行本计划。所有步骤使用 checkbox（`- [ ]`）跟踪。

**目标：** 构建确定、持久的工作流执行内核，完成静态 DAG 校验、注册工具调度、人工审批暂停、进程中断恢复、安全重试、已完成副作用补偿和脱敏审计时间线查询。

**架构：** `domain` 保存不可变工作流定义与纯状态机，`application` 负责协调，`ports` 定义稳定接口，`adapters` 提供内存、SQLite 和 Fake Tool 实现。协调器是唯一数据库写入者，工具 Worker 只返回不可变执行结果。每次持久状态转换与审计事件共用一个 Unit of Work，外部工具调用位于开始事务和结果事务之间。

**技术栈：** Python 3.12、Pydantic v2、SQLAlchemy 2.0 Core、Alembic、pytest、pytest-cov、Hypothesis。

## 全局约束

- `openspec/changes/establish-reliable-workflow-core/` 下的 OpenSpec 是行为规范的唯一事实源。
- V1 只接受静态 DAG，不实现条件路由、动态步骤、循环、LLM 调用、RAG、Web UI、Docker、PostgreSQL、分布式 Worker、多租户或生产连接。
- 单个工作流最多包含 100 个步骤和 1000 条依赖边。
- 工具并发默认值为 4，必须拒绝超过 16 的配置。
- 重试策略默认最多尝试 3 次，必须拒绝超过 10 的配置。
- SQLite 在单协调器模式下启用外键、WAL 和 5 秒 busy timeout。
- 密钥使用 `SecretRef` 表示；持久化参数、结果、错误、事件和审批快照必须脱敏。
- 外部副作用采用至少一次调度和稳定逻辑幂等键，不得宣称 exactly-once。
- 核心测试必须能在 Windows 与 WSL2 上脱离 DeepSeek、网络、Docker、PostgreSQL 和真实服务或数据库连接运行。
- Windows 开发环境使用 `C:\\Users\\kevinyuan\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe -m venv .venv` 创建 Python 3.12 虚拟环境；后续计划中的 `python` 命令均由 `.venv\\Scripts\\python.exe` 执行。

## 计划文件结构

```text
pyproject.toml
alembic.ini
src/changepilot/
|-- __init__.py
`-- workflow/
    |-- __init__.py
    |-- domain/
    |   |-- __init__.py
    |   |-- definitions.py
    |   |-- events.py
    |   |-- failures.py
    |   |-- runs.py
    |   |-- states.py
    |   `-- validation.py
    |-- application/
    |   |-- __init__.py
    |   |-- approvals.py
    |   |-- coordinator.py
    |   |-- recovery.py
    |   |-- scheduler.py
    |   `-- services.py
    |-- ports/
    |   |-- __init__.py
    |   |-- clock.py
    |   |-- identifiers.py
    |   |-- persistence.py
    |   `-- tools.py
    `-- adapters/
        |-- __init__.py
        |-- persistence/
        |   |-- __init__.py
        |   |-- memory.py
        |   |-- schema.py
        |   `-- sqlite.py
        `-- tools/
            |-- __init__.py
            `-- fake.py
alembic/
|-- env.py
`-- versions/0001_workflow_runtime.py
tests/
|-- contract/
|-- integration/
|-- process/
|-- unit/
`-- support/
```

## OpenSpec 覆盖关系

| OpenSpec 任务 | 计划任务 |
| --- | --- |
| 1.1 project baseline | 1 |
| 1.2 domain models | 1, 2 |
| 1.3 schema, registry, DAG validation | 1, 5 |
| 2.1 state transitions | 2 |
| 2.2 ports and memory adapter | 3 |
| 2.3 SQLite and migrations | 4 |
| 2.4 state/event atomicity | 3, 4 |
| 3.1 dependency scheduler | 6 |
| 3.2 tool registry and redaction | 5 |
| 3.3 idempotency and attempts | 5, 8 |
| 3.4 errors, retry, backoff, unknown result | 6, 8 |
| 4.1 approval lifecycle | 7 |
| 4.2 restart recovery | 8 |
| 4.3 compensation and manual intervention | 9 |
| 5.1 application/query interfaces | 10 |
| 5.2 end-to-end and failure tests | 10 |
| 5.3 architecture and local docs | 10 |

---

### Task 1：项目基线与不可变工作流定义

**Files:**
- Create: `pyproject.toml`
- Create: `src/changepilot/__init__.py`
- Create: `src/changepilot/workflow/__init__.py`
- Create: `src/changepilot/workflow/domain/__init__.py`
- Create: `src/changepilot/workflow/domain/definitions.py`
- Create: `src/changepilot/workflow/domain/validation.py`
- Create: `src/changepilot/workflow/ports/__init__.py`
- Create: `src/changepilot/workflow/ports/tools.py`
- Create: `tests/unit/test_definition_validation.py`

**Interfaces:**
- Produces: `WorkflowDefinition.from_mapping(payload, registry)`, `WorkflowDefinition.digest`, `StepDefinition`, `RetryPolicy`, `DefinitionValidationError`, and the read-only `ToolCatalog` protocol.
- Consumes: no project interfaces.

- [x] **Step 1: Create the Python test baseline and write failing definition tests**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "changepilot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["pydantic>=2.10,<3", "sqlalchemy>=2.0,<3", "alembic>=1.14,<2"]

[project.optional-dependencies]
dev = ["pytest>=8.3,<9", "pytest-cov>=6,<7", "hypothesis>=6.120,<7"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
```

```python
# tests/unit/test_definition_validation.py
import pytest

from changepilot.workflow.domain.validation import DefinitionValidationError
from changepilot.workflow.domain.definitions import WorkflowDefinition


class Catalog:
    def contains(self, name: str, version: str) -> bool:
        return (name, version) == ("inspect", "1")

    def validate_arguments(self, name: str, version: str, arguments: dict) -> None:
        if "target" not in arguments:
            raise ValueError("target is required")


def test_rejects_cycle_without_creating_a_definition() -> None:
    payload = {
        "definition_id": "upgrade",
        "version": 1,
        "steps": [
            {"id": "a", "tool": {"name": "inspect", "version": "1"}, "arguments": {"target": "db"}, "depends_on": ["b"]},
            {"id": "b", "tool": {"name": "inspect", "version": "1"}, "arguments": {"target": "svc"}, "depends_on": ["a"]},
        ],
    }

    with pytest.raises(DefinitionValidationError, match="cycle"):
        WorkflowDefinition.from_mapping(payload, Catalog())


def test_equivalent_payloads_have_the_same_digest() -> None:
    first = {"definition_id": "inspect", "version": 1, "steps": [{"id": "a", "tool": {"name": "inspect", "version": "1"}, "arguments": {"target": "db"}, "depends_on": []}]}
    second = {"steps": first["steps"], "version": 1, "definition_id": "inspect"}
    assert WorkflowDefinition.from_mapping(first, Catalog()).digest == WorkflowDefinition.from_mapping(second, Catalog()).digest
```

- [x] **Step 2: Install the editable project and verify RED**

Run: `C:\Users\kevinyuan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m venv .venv`

Expected: 创建使用 Python 3.12 的 `.venv`。

Run: `.venv\Scripts\python.exe -m pip install -e ".[dev]"`

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_definition_validation.py -q`

Expected: FAIL during import because `changepilot.workflow.domain.definitions` does not exist.

- [x] **Step 3: Implement immutable definitions, canonical digest, limits, tool checks, and Kahn cycle validation**

```python
# src/changepilot/workflow/domain/definitions.py
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping

from .validation import validate_definition_payload


@dataclass(frozen=True)
class ToolReference:
    name: str
    version: str


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0


@dataclass(frozen=True)
class StepDefinition:
    id: str
    tool: ToolReference
    arguments: Mapping[str, Any]
    depends_on: tuple[str, ...]
    risk: str = "low"
    retry: RetryPolicy = RetryPolicy()
    compensation_tool: ToolReference | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    definition_id: str
    version: int
    steps: tuple[StepDefinition, ...]
    digest: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], registry: object) -> "WorkflowDefinition":
        normalized = validate_definition_payload(payload, registry)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        steps = tuple(StepDefinition(
            id=item["id"],
            tool=ToolReference(**item["tool"]),
            arguments=item["arguments"],
            depends_on=tuple(item["depends_on"]),
            risk=item["risk"],
            retry=RetryPolicy(**item["retry"]),
            compensation_tool=ToolReference(**item["compensation_tool"]) if item["compensation_tool"] else None,
        ) for item in normalized["steps"])
        return cls(normalized["definition_id"], normalized["version"], steps, sha256(encoded).hexdigest())
```

Implement `validate_definition_payload()` in `validation.py` so it rejects duplicate IDs, missing dependencies, unregistered tool versions, invalid arguments, more than 100 steps, more than 1000 edges, retry counts outside `1..10`, and any cycle. It must return a newly allocated canonical dictionary with explicit defaults and steps sorted by ID.

- [x] **Step 4: Verify GREEN and full unit baseline**

Run: `python -m pytest tests/unit/test_definition_validation.py -q`

Expected: PASS, including cycle rejection and stable digest.

- [x] **Step 5: Commit Task 1**

```bash
git add pyproject.toml src/changepilot tests/unit/test_definition_validation.py
git commit -m "feat: validate immutable workflow definitions"
```

### Task 2：显式运行与步骤状态机

**Files:**
- Create: `src/changepilot/workflow/domain/states.py`
- Create: `src/changepilot/workflow/domain/runs.py`
- Create: `src/changepilot/workflow/domain/events.py`
- Create: `src/changepilot/workflow/domain/failures.py`
- Create: `tests/unit/test_state_machines.py`

**Interfaces:**
- Consumes: `WorkflowDefinition.digest` from Task 1.
- Produces: `RunState`, `StepState`, `WorkflowRun.transition()`, `StepRun.transition()`, `AuditEvent`, `ErrorClass`, and `InvalidTransition`.

- [x] **Step 1: Write failing transition-table tests**

```python
# tests/unit/test_state_machines.py
import pytest

from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.domain.failures import InvalidTransition


def test_successful_step_cannot_return_to_running() -> None:
    step = StepRun.new("run-1", "inspect").transition(StepState.READY).transition(StepState.RUNNING).transition(StepState.SUCCEEDED)
    with pytest.raises(InvalidTransition):
        step.transition(StepState.RUNNING)


def test_transition_returns_new_revision_and_event() -> None:
    run = WorkflowRun.new("run-1", "definition-1", 1, "digest")
    changed, event = run.transition(RunState.RUNNING, occurred_at="2026-07-16T00:00:00Z")
    assert run.state is RunState.PENDING
    assert changed.state is RunState.RUNNING
    assert changed.revision == 1
    assert event.event_type == "run.state_changed"
```

- [x] **Step 2: Verify RED for transition-table tests**

Run: `python -m pytest tests/unit/test_state_machines.py -q`

Expected: FAIL because the state modules do not exist.

- [x] **Step 3: Implement enums, immutable run records, transition tables, and typed failures**

```python
# src/changepilot/workflow/domain/states.py
from enum import StrEnum


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"
    MANUAL_INTERVENTION = "manual_intervention"


class StepState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RESULT_UNKNOWN = "result_unknown"
    MANUAL_INTERVENTION = "manual_intervention"
```

Define explicit `dict[State, frozenset[State]]` transition tables in `runs.py`. Use `dataclasses.replace` to return a new aggregate plus an `AuditEvent`; increment run revision exactly once for each accepted run transition. Reject every transition absent from the table.

- [x] **Step 4: Verify GREEN and exhaustive invalid-transition coverage**

Run: `python -m pytest tests/unit/test_state_machines.py -q`

Expected: PASS for every accepted edge and every rejected edge generated by parametrized tests.

- [x] **Step 5: Commit Task 2**

```bash
git add src/changepilot/workflow/domain tests/unit/test_state_machines.py
git commit -m "feat: add deterministic workflow state machines"
```

### Task 3：持久化端口、内存 Unit of Work 与事件原子性

**Files:**
- Create: `src/changepilot/workflow/ports/persistence.py`
- Create: `src/changepilot/workflow/ports/clock.py`
- Create: `src/changepilot/workflow/ports/identifiers.py`
- Create: `src/changepilot/workflow/adapters/__init__.py`
- Create: `src/changepilot/workflow/adapters/persistence/__init__.py`
- Create: `src/changepilot/workflow/adapters/persistence/memory.py`
- Create: `tests/contract/uow_contract.py`
- Create: `tests/contract/test_memory_uow.py`

**Interfaces:**
- Consumes: domain definitions, runs, steps, attempts, approvals, and events.
- Produces: `UnitOfWork`, repository protocols, `MemoryUnitOfWork`, optimistic revision checks, monotonic event sequences, and rollback semantics used by all application services.

- [x] **Step 1: Write the failing reusable Unit of Work contract**

```python
# tests/contract/uow_contract.py
import pytest

from changepilot.workflow.domain.runs import WorkflowRun
from changepilot.workflow.domain.states import RunState


def assert_state_and_event_are_atomic(uow_factory) -> None:
    run = WorkflowRun.new("run-1", "definition-1", 1, "digest")
    with uow_factory() as uow:
        uow.runs.add(run)
        uow.commit()

    with pytest.raises(RuntimeError, match="injected event failure"):
        with uow_factory(fail_event_append=True) as uow:
            current = uow.runs.get("run-1")
            changed, event = current.transition(RunState.RUNNING, "2026-07-16T00:00:00Z")
            uow.runs.save(changed, expected_revision=0)
            uow.events.append(event)
            uow.commit()

    with uow_factory() as uow:
        assert uow.runs.get("run-1").state is RunState.PENDING
        assert uow.events.list("run-1", after_sequence=0) == []
```

```python
# tests/contract/test_memory_uow.py
from changepilot.workflow.adapters.persistence.memory import MemoryStore, MemoryUnitOfWork
from .uow_contract import assert_state_and_event_are_atomic


def test_memory_state_and_event_are_atomic() -> None:
    store = MemoryStore()
    assert_state_and_event_are_atomic(lambda **kwargs: MemoryUnitOfWork(store, **kwargs))
```

- [x] **Step 2: Verify RED for the memory Unit of Work contract**

Run: `python -m pytest tests/contract/test_memory_uow.py -q`

Expected: FAIL because persistence ports and memory adapter do not exist.

- [x] **Step 3: Implement protocols and copy-on-write memory transactions**

```python
# src/changepilot/workflow/ports/persistence.py
from typing import Protocol


class UnitOfWork(Protocol):
    definitions: object
    runs: object
    steps: object
    attempts: object
    approvals: object
    events: object

    def __enter__(self) -> "UnitOfWork": ...
    def __exit__(self, exc_type, exc, traceback) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
```

`MemoryUnitOfWork` must clone committed dictionaries at entry and swap them into `MemoryStore` only after all staged validations and event appends succeed. Allocate event sequence from the transaction snapshot. `save(..., expected_revision)` raises `OptimisticLockError` if the committed revision differs.

- [x] **Step 4: Verify GREEN and repository contract tests**

Run: `python -m pytest tests/contract/test_memory_uow.py -q`

Expected: PASS for commit, rollback, optimistic locking, approval uniqueness, attempt uniqueness, monotonic sequence, and state/event atomicity.

- [x] **Step 5: Commit Task 3**

```bash
git add src/changepilot/workflow/ports src/changepilot/workflow/adapters tests/contract
git commit -m "feat: add transactional persistence ports"
```

### Task 4：SQLite Schema、Alembic 迁移与契约适配器

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_workflow_runtime.py`
- Create: `src/changepilot/workflow/adapters/persistence/schema.py`
- Create: `src/changepilot/workflow/adapters/persistence/sqlite.py`
- Create: `tests/contract/test_sqlite_uow.py`
- Create: `tests/integration/test_sqlite_configuration.py`

**Interfaces:**
- Consumes: `UnitOfWork` and repository behavior from Task 3.
- Produces: `create_sqlite_engine(path)`, `SQLiteUnitOfWork`, and a migration containing definitions, runs, steps, attempts, approvals, and events.

- [x] **Step 1: Write failing SQLite contract and connection tests**

```python
# tests/integration/test_sqlite_configuration.py
from sqlalchemy import text

from changepilot.workflow.adapters.persistence.sqlite import create_sqlite_engine


def test_sqlite_enables_required_pragmas(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "runtime.db")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000
```

`tests/contract/test_sqlite_uow.py` must invoke every function from `uow_contract.py` against a migrated temporary database.

- [x] **Step 2: Verify RED for SQLite contracts and connection configuration**

Run: `python -m pytest tests/contract/test_sqlite_uow.py tests/integration/test_sqlite_configuration.py -q`

Expected: FAIL because the SQLite adapter and migration do not exist.

- [x] **Step 3: Implement SQLAlchemy Core tables, migration, engine events, and transactional repositories**

```python
# src/changepilot/workflow/adapters/persistence/sqlite.py
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def create_sqlite_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine
```

Use SQLAlchemy Core tables with unique constraints for `(run_id, step_id)`, `(run_id, step_id, attempt_no, phase)`, `(run_id, sequence)`, and one effective pending approval per run. `SQLiteUnitOfWork` owns one connection and transaction; event append failure must roll back preceding state writes.

- [x] **Step 4: Apply migration and verify GREEN**

Run: `python -m alembic upgrade head`

Run: `python -m pytest tests/contract/test_sqlite_uow.py tests/integration/test_sqlite_configuration.py -q`

Expected: PASS with the same repository behavior as memory and all three PRAGMAs enabled.

- [x] **Step 5: Commit Task 4**

```bash
git add alembic.ini alembic src/changepilot/workflow/adapters/persistence tests/contract/test_sqlite_uow.py tests/integration/test_sqlite_configuration.py
git commit -m "feat: persist workflow runtime in sqlite"
```

### Task 5：工具注册、边界 Schema、脱敏与幂等键

**Files:**
- Modify: `src/changepilot/workflow/ports/tools.py`
- Create: `src/changepilot/workflow/application/tooling.py`
- Create: `src/changepilot/workflow/adapters/tools/__init__.py`
- Create: `src/changepilot/workflow/adapters/tools/fake.py`
- Create: `tests/unit/test_tool_registry.py`
- Create: `tests/unit/test_redaction.py`
- Create: `tests/unit/test_idempotency.py`

**Interfaces:**
- Consumes: definition-time `ToolCatalog` from Task 1.
- Produces: `ToolDescriptor`, `Tool`, `ToolRegistry`, `ToolExecutionContext`, `ToolResult`, `RecoveryResult`, `SecretRef`, `redact()`, and `logical_idempotency_key()`.

- [x] **Step 1: Write failing registration, schema, secret, and key tests**

```python
# tests/unit/test_idempotency.py
from changepilot.workflow.application.tooling import logical_idempotency_key


def test_retries_share_a_logical_idempotency_key() -> None:
    first = logical_idempotency_key("run-1", "migrate", "schema.apply", "1")
    second = logical_idempotency_key("run-1", "migrate", "schema.apply", "1")
    assert first == second
    assert "attempt" not in first


def test_compensation_uses_a_separate_namespace() -> None:
    forward = logical_idempotency_key("run-1", "migrate", "schema.apply", "1")
    compensation = logical_idempotency_key("run-1", "migrate", "schema.rollback", "1", phase="compensation")
    assert forward != compensation
```

```python
# tests/unit/test_redaction.py
from changepilot.workflow.application.tooling import SecretRef, redact


def test_redacts_declared_paths_and_secret_references() -> None:
    payload = {"token": "plain", "nested": {"password": SecretRef(provider="env", key="DB_PASSWORD")}}
    assert redact(payload, sensitive_paths={"token"}) == {"token": "[REDACTED]", "nested": {"password": {"provider": "env", "key": "[REDACTED]"}}}
```

- [x] **Step 2: Verify RED for tool boundary, redaction, and idempotency tests**

Run: `python -m pytest tests/unit/test_tool_registry.py tests/unit/test_redaction.py tests/unit/test_idempotency.py -q`

Expected: FAIL because tool boundary types are not implemented.

- [x] **Step 3: Implement typed descriptors, explicit registry, recursive redaction, fake ledger, and SHA-256 keys**

```python
# src/changepilot/workflow/ports/tools.py
from enum import StrEnum
from typing import Protocol
from pydantic import BaseModel


class IdempotencyCapability(StrEnum):
    SUPPORTED = "supported"
    PROBE_ONLY = "probe_only"
    NONE = "none"


class ToolDescriptor(BaseModel):
    name: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk: str
    idempotency: IdempotencyCapability
    timeout_seconds: float
    sensitive_paths: frozenset[str] = frozenset()


class Tool(Protocol):
    descriptor: ToolDescriptor
    def execute(self, context: object, arguments: BaseModel) -> object: ...
    def probe(self, query: object) -> object: ...
```

Reject duplicate `(name, version)`, descriptor timeouts less than or equal to zero, undeclared arguments, and registration of output models that are not Pydantic models. Fake Tool must use an independent JSON-lines ledger keyed by the logical idempotency key so process tests can count external effects.

- [x] **Step 4: Verify GREEN and secret leakage scan tests**

Run: `python -m pytest tests/unit/test_tool_registry.py tests/unit/test_redaction.py tests/unit/test_idempotency.py -q`

Expected: PASS; serialized test events and exceptions contain neither `plain` nor `DB_PASSWORD`.

- [x] **Step 5: Commit Task 5**

```bash
git add src/changepilot/workflow/ports/tools.py src/changepilot/workflow/application/tooling.py src/changepilot/workflow/adapters/tools tests/unit/test_tool_registry.py tests/unit/test_redaction.py tests/unit/test_idempotency.py
git commit -m "feat: add safe versioned tool contracts"
```

### Task 6：确定性调度器、执行尝试、重试策略与协调器

**Files:**
- Create: `src/changepilot/workflow/application/scheduler.py`
- Create: `src/changepilot/workflow/application/coordinator.py`
- Create: `tests/unit/test_scheduler.py`
- Create: `tests/integration/test_coordinator_retry.py`
- Create: `tests/support/fakes.py`

**Interfaces:**
- Consumes: state machines, Unit of Work, Tool Registry, stable keys, `Clock`, and `IdentifierFactory`.
- Produces: `ready_steps(definition, step_runs)`, `ExecutionOutcome`, `CoordinationReport`, and `Coordinator.run_once()`.

- [x] **Step 1: Write failing deterministic scheduling and retry tests**

```python
# tests/unit/test_scheduler.py
from changepilot.workflow.application.scheduler import ready_steps


def test_ready_steps_are_dependency_safe_and_stably_sorted(definition, step_runs) -> None:
    result = ready_steps(definition, step_runs)
    assert [step.id for step in result] == ["inspect-db", "inspect-service"]
```

```python
# tests/integration/test_coordinator_retry.py
def test_retryable_failure_creates_new_attempt_with_same_key(runtime) -> None:
    runtime.tools.script("inspect", ["retryable", "success"])
    runtime.coordinator.run_once()
    runtime.clock.advance(1.0)
    runtime.coordinator.run_once()
    attempts = runtime.query.attempts("run-1", "inspect")
    assert [item.number for item in attempts] == [1, 2]
    assert attempts[0].idempotency_key == attempts[1].idempotency_key
```

- [x] **Step 2: Verify RED for deterministic scheduling and coordinator retries**

Run: `python -m pytest tests/unit/test_scheduler.py tests/integration/test_coordinator_retry.py -q`

Expected: FAIL because scheduler and coordinator are absent.

- [x] **Step 3: Implement stable readiness, bounded executor, two-transaction calls, and classified retries**

```python
# src/changepilot/workflow/application/coordinator.py
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionOutcome:
    run_id: str
    step_id: str
    attempt_no: int
    status: str
    result: object | None = None
    error_class: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CoordinationReport:
    run_id: str | None
    dispatched: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    blocked_reason: str | None = None
```

`ready_steps()` must sort by topological depth then step ID. `Coordinator` validates concurrency in `1..16`, is the only database writer, writes `running + attempt + start event` before submitting a tool, and persists an `ExecutionOutcome` in a second Unit of Work. Worker functions receive immutable inputs and never receive a repository. Retryable errors enter `retry_wait` with `next_attempt_at`; permanent errors fail immediately; exhausted retries trigger the run failure policy. Use injected `FakeClock` in tests and never sleep.

- [x] **Step 4: Verify GREEN, concurrency bound, and no-stuck invariant**

Run: `python -m pytest tests/unit/test_scheduler.py tests/integration/test_coordinator_retry.py -q`

Expected: PASS for stable order, dependency gating, maximum worker capacity, retry timing, permanent failure, and internal consistency failure when a nonterminal run has no ready/in-flight work.

- [x] **Step 5: Commit Task 6**

```bash
git add src/changepilot/workflow/application tests/unit/test_scheduler.py tests/integration/test_coordinator_retry.py tests/support
git commit -m "feat: coordinate deterministic workflow execution"
```

### Task 7：持久化全局审批屏障

**Files:**
- Create: `src/changepilot/workflow/application/approvals.py`
- Create: `tests/unit/test_approval_binding.py`
- Create: `tests/integration/test_approval_barrier.py`

**Interfaces:**
- Consumes: Workflow/step state, Unit of Work, tool descriptor risk, redaction, and coordinator readiness.
- Produces: `approval_binding_digest()`, `ApprovalService.decide()`, one durable pending approval, stale-decision rejection, rejection cancellation/compensation choice, and coordinator barrier behavior.

- [ ] **Step 1: Write failing binding and barrier tests**

```python
# tests/integration/test_approval_barrier.py
def test_high_risk_ready_step_stops_new_dispatch_and_survives_restart(runtime_factory) -> None:
    runtime = runtime_factory()
    run_id = runtime.create_upgrade_run()
    runtime.drive_until_blocked(run_id)
    request = runtime.query.pending_approval(run_id)
    assert runtime.query.get_run(run_id).state == "waiting_approval"
    assert runtime.tools.calls("schema.apply") == []

    restarted = runtime_factory(existing_database=runtime.database)
    assert restarted.query.pending_approval(run_id).id == request.id
    restarted.coordinator.run_once()
    assert restarted.tools.calls("schema.apply") == []
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/unit/test_approval_binding.py tests/integration/test_approval_barrier.py -q`

Expected: FAIL because approval services and barrier logic are absent.

- [ ] **Step 3: Implement approval snapshot digest, optimistic decisions, and drain-before-barrier behavior**

```python
# src/changepilot/workflow/application/approvals.py
from hashlib import sha256
import json


def approval_binding_digest(plan_digest: str, step_id: str, tool_name: str, tool_version: str, redacted_arguments: object) -> str:
    payload = {"plan_digest": plan_digest, "step_id": step_id, "tool": [tool_name, tool_version], "arguments": redacted_arguments}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
```

When any high-risk step becomes ready, stop dispatching new forward work, drain already submitted futures, atomically create the approval request plus event, and transition the run to `waiting_approval`. Decisions require `expected_version`; stale, duplicate, mismatched-digest, or already-decided requests are rejected. Rejection before side effects cancels; rejection after compensable successes starts compensation.

- [ ] **Step 4: Verify GREEN and approval race coverage**

Run: `python -m pytest tests/unit/test_approval_binding.py tests/integration/test_approval_barrier.py -q`

Expected: PASS for restart, approval, rejection, changed arguments, changed tool version, duplicate decisions, and optimistic-lock races.

- [ ] **Step 5: Commit Task 7**

```bash
git add src/changepilot/workflow/application/approvals.py src/changepilot/workflow/application/coordinator.py tests/unit/test_approval_binding.py tests/integration/test_approval_barrier.py
git commit -m "feat: enforce durable approval barriers"
```

### Task 8：崩溃恢复、探测语义与未知结果

**Files:**
- Create: `src/changepilot/workflow/application/recovery.py`
- Create: `tests/integration/test_recovery.py`
- Create: `tests/process/worker_scenario.py`
- Create: `tests/process/test_process_recovery.py`

**Interfaces:**
- Consumes: persisted incomplete attempts, tool idempotency capability, Tool.probe, coordinator, SQLite adapter, and Fake Tool ledger.
- Produces: `RecoveryService.recover_nonterminal_runs()`, probe-first recovery, result-unknown handling, and subprocess fault injection points.

- [ ] **Step 1: Write failing subprocess recovery test for side-effect-before-result-commit**

```python
# tests/process/test_process_recovery.py
import json
import subprocess
import sys


def test_restart_probes_existing_effect_without_repeating_it(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    ledger = tmp_path / "effects.jsonl"
    first = subprocess.run([sys.executable, "-m", "tests.process.worker_scenario", "--db", str(database), "--ledger", str(ledger), "--crash-at", "after-effect-before-commit"])
    assert first.returncode == 91

    second = subprocess.run([sys.executable, "-m", "tests.process.worker_scenario", "--db", str(database), "--ledger", str(ledger), "--crash-at", "never"])
    assert second.returncode == 0
    effects = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len([item for item in effects if item["step_id"] == "migrate-schema"]) == 1
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/integration/test_recovery.py tests/process/test_process_recovery.py -q`

Expected: FAIL because startup recovery and the process scenario are absent.

- [ ] **Step 3: Implement recovery scan, incomplete-attempt probe, and manual-intervention rules**

```python
# src/changepilot/workflow/application/recovery.py
class RecoveryService:
    def __init__(self, uow_factory, tools, clock) -> None:
        self._uow_factory = uow_factory
        self._tools = tools
        self._clock = clock

    def recover_nonterminal_runs(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for run_id in self._list_nonterminal_run_ids():
            self._recover_run(run_id)
            recovered.append(run_id)
        return tuple(recovered)
```

For an incomplete running attempt, call `probe` with the persisted logical key before any execute call. Probe outcomes are `applied(result)`, `not_applied`, and `unknown`. Applied persists success; not-applied returns an idempotent step to ready; unknown or a non-probeable tool moves the step and run to `manual_intervention`. Record every recovery choice as a redacted event. Implement fault points before execute, after external ledger write before result commit, while waiting approval, and after compensation effect before commit using `os._exit(91)` only inside the subprocess fixture.

- [ ] **Step 4: Verify GREEN for all restart windows**

Run: `python -m pytest tests/integration/test_recovery.py tests/process/test_process_recovery.py -q`

Expected: PASS with one external ledger effect per logical step, the same approval request after restart, and manual intervention for a non-idempotent unknown result.

- [ ] **Step 5: Commit Task 8**

```bash
git add src/changepilot/workflow/application/recovery.py tests/integration/test_recovery.py tests/process
git commit -m "feat: recover interrupted workflow attempts"
```

### Task 9：逆依赖补偿与失败信息保留

**Files:**
- Modify: `src/changepilot/workflow/application/scheduler.py`
- Modify: `src/changepilot/workflow/application/coordinator.py`
- Create: `tests/unit/test_compensation_order.py`
- Create: `tests/integration/test_compensation.py`

**Interfaces:**
- Consumes: successful `StepRun.completion_sequence`, DAG dependencies, compensation tool references, attempt phases, and recovery probe semantics.
- Produces: `compensation_order()`, compensation attempts and keys, `compensated` terminal state, and `manual_intervention` preserving original and compensation failures.

- [ ] **Step 1: Write failing ordering and dual-error tests**

```python
# tests/unit/test_compensation_order.py
from changepilot.workflow.application.scheduler import compensation_order


def test_compensation_reverses_dependencies_then_completion_sequence() -> None:
    dependencies = {"migrate": (), "deploy": ("migrate",), "notify": ()}
    completed = {"migrate": 1, "notify": 2, "deploy": 3}
    assert compensation_order(dependencies, completed) == ("deploy", "notify", "migrate")
```

```python
# tests/integration/test_compensation.py
def test_compensation_failure_preserves_both_errors(runtime) -> None:
    runtime.tools.script("health.check", ["permanent"])
    runtime.tools.script("service.restore", ["permanent"])
    run_id = runtime.create_upgrade_run(approved=True)
    runtime.drive_to_terminal(run_id)
    view = runtime.query.get_run(run_id)
    assert view.state == "manual_intervention"
    assert view.original_error.code == "health_check_failed"
    assert view.compensation_error.code == "service_restore_failed"
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/unit/test_compensation_order.py tests/integration/test_compensation.py -q`

Expected: FAIL because compensation ordering and coordinator mode are absent.

- [ ] **Step 3: Implement reverse-topological compensation with independent attempts and recovery**

```python
# src/changepilot/workflow/application/scheduler.py
def compensation_order(dependencies: dict[str, tuple[str, ...]], completion_sequence: dict[str, int]) -> tuple[str, ...]:
    remaining = set(completion_sequence)
    ordered: list[str] = []
    while remaining:
        candidates = [step for step in remaining if not any(step in dependencies.get(other, ()) for other in remaining)]
        selected = max(candidates, key=lambda step: (completion_sequence[step], step))
        ordered.append(selected)
        remaining.remove(selected)
    return tuple(ordered)
```

The coordinator enters `compensating` after a forward failure when at least one successful step has a compensation tool. Persist compensation attempts with phase `compensation`, a separate stable key namespace, and the same two-transaction/probe behavior. Never overwrite `original_error`; write `compensation_error` separately. Complete as `compensated` only after every required compensation succeeds.

- [ ] **Step 4: Verify GREEN including interrupted compensation**

Run: `python -m pytest tests/unit/test_compensation_order.py tests/integration/test_compensation.py tests/process/test_process_recovery.py -q`

Expected: PASS for reverse order, unrelated completion order, no compensation for unsuccessful steps, compensation restart, compensated terminal state, and dual-error manual intervention.

- [ ] **Step 5: Commit Task 9**

```bash
git add src/changepilot/workflow/application/scheduler.py src/changepilot/workflow/application/coordinator.py tests/unit/test_compensation_order.py tests/integration/test_compensation.py tests/process/test_process_recovery.py
git commit -m "feat: compensate completed workflow effects"
```

### Task 10：应用服务、订单升级验收场景与文档

**Files:**
- Create: `src/changepilot/workflow/application/services.py`
- Create: `tests/support/order_upgrade.py`
- Create: `tests/integration/test_order_upgrade_acceptance.py`
- Create: `tests/unit/test_query_service.py`
- Create: `README.md`
- Create: `docs/architecture/reliable-workflow-core.md`
- Modify: `openspec/changes/establish-reliable-workflow-core/tasks.md`

**Interfaces:**
- Consumes: all domain, application, port, and adapter interfaces from Tasks 1-9.
- Produces: `WorkflowService.create_run()`, `WorkflowService.cancel_run()`, `ApprovalService.decide()`, `QueryService.get_run()`, `QueryService.list_events()`, the fixed order-upgrade definition, and documented local commands.

- [ ] **Step 1: Write failing public-service and three-path acceptance tests**

```python
# tests/integration/test_order_upgrade_acceptance.py
def test_order_upgrade_succeeds_after_approval(runtime) -> None:
    run_id = runtime.workflow_service.create_run(runtime.order_upgrade_definition())
    runtime.drive_until_approval(run_id)
    request = runtime.query_service.get_run(run_id).pending_approval
    runtime.approval_service.decide(request.id, "approved", request.version)
    runtime.drive_to_terminal(run_id)
    assert runtime.query_service.get_run(run_id).state == "succeeded"


def test_health_failure_compensates_service_then_schema(runtime) -> None:
    runtime.tools.script("health.check", ["permanent"])
    run_id = runtime.create_approved_order_upgrade()
    runtime.drive_to_terminal(run_id)
    assert runtime.query_service.get_run(run_id).state == "compensated"
    assert runtime.tools.compensation_calls() == ["service.restore", "schema.rollback"]


def test_audit_timeline_is_ordered_and_redacted(runtime) -> None:
    run_id = runtime.create_successful_order_upgrade(secret="do-not-persist")
    events = runtime.query_service.list_events(run_id, after_sequence=0)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert "do-not-persist" not in repr(events)
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/unit/test_query_service.py tests/integration/test_order_upgrade_acceptance.py -q`

Expected: FAIL because public application services and the fixed scenario are absent.

- [ ] **Step 3: Implement application views/services and the exact acceptance DAG**

```python
# src/changepilot/workflow/application/services.py
class WorkflowService:
    def __init__(self, uow_factory, tool_catalog, identifiers, clock) -> None:
        self._uow_factory = uow_factory
        self._tool_catalog = tool_catalog
        self._identifiers = identifiers
        self._clock = clock

    def create_run(self, definition_payload: dict) -> str:
        definition = WorkflowDefinition.from_mapping(definition_payload, self._tool_catalog)
        run_id = self._identifiers.new_run_id()
        with self._uow_factory() as uow:
            uow.definitions.add(definition)
            uow.runs.add(WorkflowRun.new(run_id, definition.definition_id, definition.version, definition.digest))
            uow.steps.add_all(StepRun.new(run_id, step.id) for step in definition.steps)
            uow.events.append(AuditEvent.run_created(run_id, self._clock.now(), definition.digest))
            uow.commit()
        return run_id
```

The fixed DAG is `inspect-service` and `inspect-db` to `precheck`, then approved `migrate-schema`, `deploy-v2`, `health-check`, and `smoke-test`. Query views are immutable Pydantic DTOs and never expose repository/domain mutation methods. `list_events(after_sequence)` returns ascending sequence and redacted payloads.

- [ ] **Step 4: Run complete verification and coverage**

Run: `python -m pytest -q`

Expected: PASS for all unit, contract, integration, property, and process tests.

Run: `python -m pytest --cov=changepilot.workflow --cov-report=term-missing --cov-fail-under=85 -q`

Expected: PASS with at least 85% statement coverage and no warning or secret output.

Run: `openspec validate establish-reliable-workflow-core --strict --json --no-interactive`

Expected: one valid change, zero issues.

- [ ] **Step 5: Document architecture, local execution, guarantees, and non-goals**

`README.md` must contain installation, migration, test, and fixed-scenario commands. `docs/architecture/reliable-workflow-core.md` must include the package dependency rule, transaction boundaries, recovery decision table, approval binding, compensation semantics, and the explicit statement that the core is Phase 1 of ChangePilot rather than the complete Agent loop.

- [ ] **Step 6: Check off OpenSpec tasks only after matching evidence exists**

Run: `python -m pytest -q`

Expected: PASS immediately before changing checkboxes. Change all 17 task markers in `openspec/changes/establish-reliable-workflow-core/tasks.md` from `[ ]` to `[x]` only when their linked tests or documentation exist.

- [ ] **Step 7: Commit Task 10**

```bash
git add src/changepilot/workflow/application/services.py tests/support/order_upgrade.py tests/integration/test_order_upgrade_acceptance.py tests/unit/test_query_service.py README.md docs/architecture/reliable-workflow-core.md openspec/changes/establish-reliable-workflow-core/tasks.md
git commit -m "feat: complete reliable workflow core acceptance"
```

## 最终构建门禁

- [ ] Run `python -m pytest -q` and confirm zero failures.
- [ ] Run `python -m pytest --cov=changepilot.workflow --cov-report=term-missing --cov-fail-under=85 -q` and confirm the threshold passes.
- [ ] Run `openspec validate establish-reliable-workflow-core --strict --json --no-interactive` and confirm zero issues.
- [ ] Run `git status --short` and account for every remaining change without touching ignored personal files.
- [ ] Request the configured code review before `comet guard establish-reliable-workflow-core build --apply`.
