# ChangePilot

[简体中文](README.zh-CN.md)

ChangePilot is a bounded change-planning Agent backed by a reliable workflow
runtime. It turns a service upgrade request into an evidence-cited DAG, applies
deterministic risk and compensation policies, pauses for clarification or
approval when needed, and then hands the validated plan to a durable executor.

The reference scenario upgrades an order service and applies its database
schema migration:

```text
request -> retrieve runbooks -> generate plan -> validate and repair at most once
        -> prepare workflow -> inspect service + database -> precheck
        -> approval -> migrate -> deploy -> verify -> compensate on failure
```

This is an Agent because it closes a bounded perception/reasoning/action loop:
it gathers context, uses a model to propose a goal-directed plan, checks the
plan against tools and policies, can ask for missing information or repair an
invalid response, and delegates effects to a guarded runtime. RAG and LangGraph
support that loop; they are not what makes the system an Agent by themselves.

## Requirements

- Python 3.12
- Windows, WSL, or Linux
- No Docker requirement
- A DeepSeek key only for the opt-in live smoke test

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m alembic upgrade head
```

Install optional local BGE embeddings for hybrid retrieval:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,retrieval]"
```

Without that extra, SQLite FTS5 keyword retrieval remains available and all
default tests run offline.

## Offline Agent Demo

The executable acceptance test uses the sample Runbooks in
`examples/runbooks`, a deterministic Mock LLM, and the real Phase 1 runtime:

```powershell
.venv\Scripts\python.exe -m pytest tests\planning\acceptance\test_order_upgrade_planning.py -q
```

It proves that the Agent retrieves evidence, produces a seven-step plan,
promotes the schema migration to high risk, converts the plan to a runtime DAG,
executes the read-only prechecks, and stops before `schema.migrate` with a
pending approval. No network or paid API is used.

Run the prompt-injection boundary test with:

```powershell
.venv\Scripts\python.exe -m pytest tests\planning\security\test_prompt_injection.py -q
```

## Offline Execution Sandbox

The execution sandbox continues from the approved plan and applies real local
effects to an isolated V1 order service and SQLite database. Replay a successful
upgrade, a failed health check with reverse compensation, or a process restart
after the schema migration committed but before its result was acknowledged:

```powershell
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario success --root .demo\success
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario compensation --root .demo\compensation
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario recovery --root .demo\recovery
```

Each command prints a JSON summary of the final workflow and sandbox state.
They use local Python and SQLite only: no DeepSeek API, Docker, PostgreSQL, or
network access is required.

## Operations API

Phase 4 adds a local operator-facing API for structured change requests, plan
review, approval or rejection, run snapshots, cursor-based audit events, SSE,
recovery, and server-generated reports:

```powershell
$env:CHANGEPILOT_OPERATIONS_ROOT = ".operations"
.venv\Scripts\python.exe -m uvicorn changepilot.operations.server:app --reload
```

Open `http://127.0.0.1:8000/docs` for the resource API. The V1 endpoint drives
the isolated order-service scenarios and never accepts arbitrary SQL, shell
commands, tool calls, or production paths.

## Offline Evaluation

The versioned core dataset contains development and holdout cases for planning,
approval blocking, successful execution, compensation, and restart recovery.
Run it with the Mock LLM and local SQLite sandbox:

```powershell
.venv\Scripts\python.exe -m changepilot.evaluation.cli `
  --dataset examples\evaluations\core-v1.json `
  --output .eval-results\core-v1
```

The command produces comparable JSON and Markdown reports and exits non-zero
when a declared safety threshold regresses. It does not read a DeepSeek key or
make network requests.

## Runbook Updates

Runbooks are Markdown files with YAML frontmatter:

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

Load and ingest a version with the application API:

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

`document_id` plus `version` is immutable. Change the version when content
changes; re-ingesting identical content is idempotent. Planning records the
knowledge snapshot digest and refuses execution preparation if the snapshot has
changed since the plan was generated.

## DeepSeek Configuration

The provider uses an OpenAI-compatible adapter. Configuration is read from:

```powershell
$env:CHANGEPILOT_DEEPSEEK_API_KEY = "..."
$env:CHANGEPILOT_LLM_BASE_URL = "https://api.deepseek.com"
$env:CHANGEPILOT_LLM_MODEL = "deepseek-v4-flash"
$env:CHANGEPILOT_LLM_TIMEOUT_SECONDS = "30"
```

`BudgetedModelGateway` enforces call, token, and optional estimated-cost limits,
caches identical requests, and allows a bounded retry count. The live smoke
test is skipped by default. To spend at most one small request deliberately:

```powershell
$env:CHANGEPILOT_RUN_LIVE_LLM = "1"
.venv\Scripts\python.exe -m pytest tests\planning\live\test_deepseek_smoke.py -q
```

## Verification

```powershell
.venv\Scripts\python.exe -m pytest tests\planning -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\sandbox -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\operations -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest --cov=changepilot --cov-report=term-missing --cov-fail-under=85 -q -p no:cacheprovider
openspec validate add-change-execution-sandbox --strict --json --no-interactive
git diff --check
```

See `docs/architecture/agent-planning-and-knowledge.md` for the Agent boundary
and `docs/architecture/change-execution-sandbox.md` for the local execution and
recovery scenarios. Runtime transactions, recovery, approval, compensation,
and audit behavior are documented in
`docs/architecture/reliable-workflow-core.md`.

## Contributing

Development setup, branch conventions, repository boundaries, and pull request
expectations are documented in `CONTRIBUTING.md`.
