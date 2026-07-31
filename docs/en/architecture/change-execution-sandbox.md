# Change Execution Sandbox

## Purpose

The execution sandbox turns ChangePilot's validated order-service upgrade plan
into observable local effects. It demonstrates the part of an enterprise
change Agent that sits between plan generation and production adapters:

```text
validated plan
  -> inspect service and schema
  -> run compatibility precheck
  -> wait for high-risk approval
  -> migrate schema
  -> deploy service V2
  -> validate health and order behavior
  -> compensate in reverse order when validation fails
```

This is intentionally more than a prompt demo. The model may help produce a
plan, but deterministic code owns authorization, tool arguments, effects,
idempotency, recovery, compensation, and audit history.

## Boundaries

The `changepilot.sandbox` package has four layers:

- `domain` defines strict tool arguments, outputs, versions, state, and faults.
- `application` manages isolated environments, the order-service facade,
  deterministic fault injection, and runtime assembly.
- `adapters` implements SQLite state and versioned workflow tools.
- `demo` provides three repeatable command-line scenarios.

Each sandbox is addressed by a restricted identifier and stored beneath a
configured base directory. Tools receive a `sandbox_id`; they cannot accept a
filesystem path, SQL statement, shell command, or network destination. Resource
resolution rejects absolute paths and traversal outside the selected sandbox.
Reset removes only the known state files.

The local sandbox contains:

```text
<root>/
  workflow.db
  sandboxes/order-demo/
    orders.db
    sandbox.json
    faults.json
```

`workflow.db` stores durable workflow checkpoints and audit events. `orders.db`
stores the V1/V2 order schema, migration ledger, and tool-effect ledger.
`sandbox.json` records the deployed service version. `faults.json` persists
fault rules and call counters across process restarts.

## Fixed Upgrade

The executable workflow contains seven steps:

| Step | Tool | Role |
| --- | --- | --- |
| `inspect-service` | `service.inspect` | Read deployed service version |
| `inspect-db` | `schema.inspect` | Read schema version and fingerprint |
| `precheck` | `upgrade.precheck` | Verify V1 baseline compatibility |
| `migrate-schema` | `schema.migrate` | Apply the high-risk V1 to V2 migration |
| `deploy-v2` | `service.deploy-v2` | Switch the service after schema migration |
| `health-check` | `service.health-check` | Verify service/database compatibility |
| `smoke-test` | `service.smoke-test` | Exercise the versioned order contract |

The migration step requires approval and an expected database fingerprint.
The migration and deployment tools persist logical idempotency keys. Repeating
an acknowledged call returns its previous effect instead of applying it twice.

Compensation metadata maps `service.deploy-v2` to `service.restore` and
`schema.migrate` to `schema.rollback`. The workflow compensates in reverse
completion order. Schema rollback also refuses to discard V2-only business
data, so compensation is bounded by an explicit safety policy.

## Recovery Semantics

Fault injection distinguishes where execution was interrupted:

- `before_effect`: no external state changed. Recovery probes `NOT_FOUND`, then
  retries with the same logical idempotency key.
- `after_effect`: state changed but the result was not acknowledged. Recovery
  probes `FOUND`, records the effect as completed, and does not execute it again.
- `permanent_failure`: the tool reports a non-retryable error and the workflow
  starts compensation for completed reversible steps.

Recovery uses both durable workflow checkpoints and the actual sandbox state.
This prevents a restart from trusting only one side of a partially completed
operation.

## Replay

All scenarios use only local Python and SQLite:

```powershell
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario success --root .demo\success
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario compensation --root .demo\compensation
.venv\Scripts\python.exe -m changepilot.sandbox.demo --scenario recovery --root .demo\recovery
```

The JSON result reports the terminal workflow state, service and schema
versions, migration ledger, and audit event count. The recovery scenario also
reports that the committed migration was invoked only once.

These scenarios require no DeepSeek API key, Docker daemon, PostgreSQL server,
or network connection. SQLite is the reference adapter so the execution
semantics can be reviewed and tested cheaply.

## Production Adapter Path

A PostgreSQL or Docker-backed implementation would replace the sandbox
database and service adapters while keeping the same versioned tool contracts,
risk policy, idempotency keys, probes, compensation metadata, and workflow
events. A production integration would additionally need:

- secret management and workload identity;
- real deployment and database migration APIs;
- distributed locking and stronger concurrency controls;
- organization-specific approval and authorization;
- backups, point-in-time recovery, observability, and retention policies.

The local adapter does not claim production isolation or distributed
transaction guarantees. Its purpose is to make the Agent's execution contract,
failure behavior, and recovery decisions concrete before connecting privileged
infrastructure.
