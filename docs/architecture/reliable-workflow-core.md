# Reliable Workflow Core Architecture

## Scope

This package is the reliable workflow core for ChangePilot Phase 1. It turns a
validated, fixed workflow definition into durable runs, guarded tool effects,
recovery decisions, compensation, and queryable audit history. It is not the
complete ChangePilot Agent loop and does not include a Web API, UI, prompt
planning, or LLM integration.

## Dependency rule

Dependencies point inward:

1. `domain` owns workflow definitions, run/step state machines, events, and
   failure classifications. It may use stable contracts from `ports` but has no
   dependency on application services or adapters.
2. `ports` defines clocks, identifiers, persistence repositories/unit of work,
   and tool contracts.
3. `application` composes domain behavior through ports. `WorkflowService`,
   `ApprovalService`, `RecoveryService`, `Coordinator`, `QueryService`, and
   `WorkflowDriver` live here.
4. `adapters` implement persistence and tools. They depend on domain and ports;
   application code never imports a concrete adapter.

Public reads cross the application boundary as frozen Pydantic DTOs. DTOs copy
and recursively freeze nested payloads, expose no repositories or aggregate
mutation methods, and report states as values rather than mutable domain
objects.

## Transaction boundaries

- `WorkflowService.create_run()` validates the definition before opening a
  transaction, then atomically writes the definition (or verifies an identical
  existing version), run, all step records, and `workflow.created` event.
- `WorkflowService.cancel_run()` applies the domain state machine and atomically
  saves the run transition and its audit event.
- Approval creation/decision, attempt start, attempt outcome, recovery decision,
  and compensation outcome each use their own explicit unit-of-work commit.
- Tool execution is outside the database transaction. The committed attempt and
  logical idempotency key exist before dispatch; the outcome is persisted in a
  later transaction.
- Run and step updates use optimistic revisions. Audit sequence allocation is
  committed with the state change it describes.

SQLite is the durable Phase 1 adapter. WAL mode, foreign keys, and a busy timeout
are configured by the adapter. Alembic owns schema evolution.

## Startup and recovery

`WorkflowDriver` is the process-facing lifecycle boundary. `start()` invokes
`RecoveryService.recover_nonterminal_runs()` and only marks the driver ready
after recovery returns. `run_once()` rejects dispatch before that point. Every
process restart must construct the services and call `start()` before polling
the coordinator.

| Persisted observation | Recovery decision | Result |
| --- | --- | --- |
| Tool probe finds the logical effect | Applied | Persist successful outcome and continue/finish |
| Tool probe proves no effect | Not applied | Return the forward step to ready, or retry compensation |
| Tool cannot probe, probe fails, or result is inconclusive | Unknown | Move run (and forward step) to manual intervention |
| No unresolved running/result-unknown attempt | No action | Coordinator may evaluate normal dispatch |

The coordinator independently refuses to dispatch while persisted recovery work
exists. This is a second guard, not a replacement for startup recovery.

## Approval binding

High-risk steps stop at a single pending approval barrier. The request binds the
definition digest, step ID, exact tool name/version, and canonical redacted
arguments into a SHA-256 digest. A decision must match the current request
version and binding. Changed definitions, tools, or arguments invalidate the old
approval and require a new request. Approval actor and reason are stored as
domain-separated SHA-256 digests, never as operator-supplied plaintext.

Approval only authorizes the bound step. It does not authorize later high-risk
steps or bypass unresolved recovery work.

## Compensation

A permanent forward failure records `original_error`. If completed forward
attempts report applied effects and define compensation tools, the run enters
`compensating`. Compensation follows deterministic reverse dependency order and
uses a distinct phase and logical idempotency key. In the fixed order-upgrade
scenario, `service.restore` runs before `schema.rollback`.

Successful rollback ends in `compensated`. A permanent or unprovable
compensation result ends in `manual_intervention` while retaining both
`original_error` and `compensation_error` for diagnosis.

## Audit and redaction

Events are run-local and receive contiguous, strictly increasing sequence
numbers at commit. `QueryService.list_events(run_id, after_sequence)` implements
an exclusive cursor: every returned sequence is greater than the supplied
value. State and event publication share the same transaction.

Tool arguments and outputs are redacted by declared sensitive paths and common
sensitive field names. Secret references persist only as placeholders. Approval
actor/reason text is digested. Query DTOs apply a final redaction pass and freeze
payloads before returning them. Raw secrets must not appear in DTOs, event
payloads, structured persistence fields, SQLite pages, or WAL content.

## Phase 1 limits and non-goals

- One active coordinator process owns dispatch. Multi-coordinator leasing and
  distributed scheduling are not provided.
- SQLite is the supported durable store; this is not a distributed database
  design.
- Tool execution is bounded and idempotency-aware, but external tools remain
  responsible for truthful execution/probe semantics.
- There is no Web/API transport, authentication layer, user interface, Docker
  requirement, LLM planner, prompt execution, or autonomous Agent loop.
- Workflow definitions are supplied by trusted application code and validated
  before persistence; dynamic LLM-authored workflow generation is out of scope.
