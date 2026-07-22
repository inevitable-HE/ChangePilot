# ChangePilot Reliable Workflow Core

ChangePilot Phase 1 is a durable workflow execution core for validated DAGs. It
provides transactional run creation, bounded tool dispatch, approval barriers,
crash recovery, compensation, immutable query DTOs, and ordered redacted audit
events. It is not a Web application or a complete LLM/Agent loop.

## Requirements and installation

- Python 3.12
- No Docker requirement

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

WSL or another POSIX shell:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## Database migration

The default Alembic URL in `alembic.ini` targets `workflow-runtime.db` in the
repository root. Override the URL in deployment configuration when a different
SQLite location is required.

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
```

```bash
.venv/bin/python -m alembic upgrade head
```

Application startup must call `WorkflowDriver.start()` before `run_once()`.
Startup runs `RecoveryService.recover_nonterminal_runs()` first; only after it
finishes can the coordinator dispatch work.

## Fixed acceptance scenario

The executable acceptance scenario upgrades an order service with this DAG:

```text
inspect-service + inspect-db
            -> precheck
            -> migrate-schema (approval required; schema.rollback)
            -> deploy-v2 (service.restore)
            -> health-check
            -> smoke-test
```

The tests demonstrate approval-gated success and permanent health-check failure
with compensation in the order `service.restore`, then `schema.rollback`.

## Verification

Windows PowerShell commands are shown below. In WSL, replace
`.venv\Scripts\python.exe` with `.venv/bin/python`.

Current verified baseline (2026-07-22):

- 384 tests passed
- 92% statement coverage (85% required)
- OpenSpec strict validation passed with zero issues

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_query_service.py tests\integration\test_order_upgrade_acceptance.py -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest --cov=changepilot.workflow --cov-report=term-missing --cov-fail-under=85 -q -p no:cacheprovider
openspec validate establish-reliable-workflow-core --strict --json --no-interactive
git diff --check
```

See `docs/architecture/reliable-workflow-core.md` for package boundaries,
transaction semantics, recovery decisions, and Phase 1 limitations.
The detailed verification record is available at
`docs/superpowers/reports/2026-07-22-establish-reliable-workflow-core-verify.md`.

## Contributing

Development setup, branch conventions, repository boundaries, and pull request
expectations are documented in `CONTRIBUTING.md`.
