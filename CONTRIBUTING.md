# Contributing to ChangePilot

ChangePilot accepts changes through short-lived branches and pull requests. Keep
each pull request focused on one behavior or architectural concern so that it is
straightforward to review, test, and revert.

## Local setup

Use Python 3.12. Docker and an LLM API are not required for the reliable workflow
core.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

On WSL or another POSIX shell, use `.venv/bin/python` instead.

## Development workflow

1. Create a branch from the latest `master` using a descriptive prefix such as
   `feature/`, `fix/`, `docs/`, or `chore/`.
2. Add or update tests before changing behavior. Include failure and recovery
   paths for workflow execution changes.
3. Run the full verification command locally.
4. Open a pull request using the repository template and keep it current with
   `master`.
5. Resolve review conversations and wait for required CI checks before merging.

```powershell
.venv\Scripts\python.exe -m pytest --cov=changepilot.workflow --cov-report=term-missing --cov-fail-under=85 -q
```

## Repository boundaries

Commit source code, tests, database migrations, architecture documentation, and
OpenSpec planning artifacts that explain product behavior. Do not commit local
Agent skills, Codex/Comet runtime state, virtual environments, databases,
credentials, generated coverage files, or personal interview materials.

## Pull request expectations

- Explain both the behavior change and why it is needed.
- Keep public interfaces and persistence migrations backward-aware.
- Preserve audit redaction, approval binding, idempotency, and recovery safety.
- Update README or architecture documentation when setup, guarantees, or
  non-goals change.
- Never include raw secrets in fixtures, logs, events, screenshots, or examples.
