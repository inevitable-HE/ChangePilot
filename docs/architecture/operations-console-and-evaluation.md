# Operations Console and Evaluation

## Purpose

Phase 4 turns the planning and execution runtime into an operator-facing
closed loop:

```text
structured request
  -> plan and evidence review
  -> deterministic approval gate
  -> live execution and audit events
  -> compensation or explicit recovery
  -> terminal report
  -> repeatable safety evaluation
```

The console does not execute tools or modify workflow persistence directly.
FastAPI is the authoritative command boundary; the React application is a
query and command client.

## Operator API

`OperationsService` owns request validation, fixed demo-plan construction,
sandbox runtime assembly, command validation, report generation, and
redaction. The API exposes:

- structured request submission and request history;
- run lists, snapshots, plan annotations, and cursor-based events;
- version-bound approval or rejection;
- revision-bound recovery;
- SSE audit delivery with polling as a client fallback;
- server-generated JSON and Markdown reports;
- read-only versioned evaluation history.

The API accepts a scenario identifier, not arbitrary SQL, shell commands,
filesystem paths, or tool arguments. Approval commands carry the current
approval version and binding digest. Recovery commands carry the current run
revision. Stale or illegal commands return a conflict and leave the
authoritative state unchanged.

## Console

The operations workspace has three responsibilities:

1. Select or submit an isolated change run.
2. Review the DAG, dependencies, risk, validation intent, evidence, and
   compensation before approving a side effect.
3. Follow ordered audit events, inspect payloads, recover unknown results, and
   export terminal reports.

The console consumes SSE events after the latest observed sequence and
periodically reconciles with the complete server snapshot. This makes event
delivery useful for responsiveness without making it the source of truth.

## Evaluation

The core evaluation dataset is versioned independently from code and records
the prompt, knowledge, metrics, seed, development split, and holdout split.
The offline runner uses the deterministic mock plan and the real local
workflow/sandbox boundaries. It measures:

- plan validity, required and forbidden actions, risk, and evidence;
- approval blocking, idempotency, compensation, recovery, and completion;
- latency, calls, tokens, cache hits, and estimated cost.

`examples/evaluations/baseline-v1/evaluation.json` is the first committed
passing baseline. The result page shows aggregate safety metrics, case-level
failures, budget telemetry, regressions, and compatible baseline deltas.

## Online Cost Boundary

Online evaluation is disabled unless
`CHANGEPILOT_RUN_ONLINE_EVAL=1` is explicitly set and a DeepSeek key is
available. The online harness requires a usage estimate before each call and
reserves sample, call, token, and estimated-cost budgets before invoking the
provider. If any reservation would exceed a limit, remaining cases are
skipped and a partial outcome records completed cases, skipped cases, usage,
and the stop reason. Credentials are used only to construct the provider call
and are never included in evaluation or operations reports.

## Local Demonstration

Start the API and console as documented in the README, then:

1. Create a successful order-service upgrade.
2. Open `migrate-schema` and review its runbook references and rollback tool.
3. Approve the pending schema migration and observe the run reach `succeeded`.
4. Create a health-failure scenario to observe reverse compensation.
5. Create a restart-recovery scenario to reconcile a committed migration
   without applying it twice.
6. Open Evaluations to inspect the offline baseline and zero-cost telemetry.

This local implementation demonstrates control-plane semantics. Production
use still requires organization-specific identity, authorization, secret
management, distributed coordination, deployment adapters, database backup
policy, observability, and retention controls.
