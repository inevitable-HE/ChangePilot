# ChangePilot

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
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest --cov=changepilot --cov-report=term-missing --cov-fail-under=85 -q -p no:cacheprovider
openspec validate add-agent-planning-and-knowledge --strict --json --no-interactive
git diff --check
```

See `docs/architecture/agent-planning-and-knowledge.md` for the Agent boundary
and `docs/architecture/reliable-workflow-core.md` for runtime transactions,
recovery, approval, compensation, and audit behavior.

## Contributing

Development setup, branch conventions, repository boundaries, and pull request
expectations are documented in `CONTRIBUTING.md`.
