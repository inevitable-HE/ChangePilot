# Agent Planning and Knowledge Architecture

## Scope

Phase 2 adds a bounded planning Agent in front of the Phase 1 reliable workflow
runtime. Its responsibility ends when it has produced and persisted a validated
`PreparedWorkflow`. It never executes change tools directly.

The resulting closed loop is:

1. Normalize a structured change request.
2. Ask for required context that is missing.
3. Retrieve versioned Runbook evidence.
4. Ask the model for a structured change plan.
5. Validate tools, arguments, dependencies, evidence, risk, approval, and
   compensation with deterministic code.
6. Repair malformed model output at most once, or return a terminal result.
7. Bind the accepted plan to its knowledge snapshot and convert it to a runtime
   workflow.
8. Let the reliable runtime execute tools, stop at approvals, recover after
   interruption, compensate failed effects, and record an audit trail.

RAG supplies evidence and LangGraph expresses the bounded planning states. The
Agent behavior comes from the complete feedback loop and the authority
boundaries around it, not from either library alone.

## Planning Graph

```text
normalize
   |
required context --missing--> clarification_required
   |
retrieve --conflict--> clarification_required
   |
generate --budget--> budget_exhausted
   |        \--provider failure--> planning_rejected
validate --policy violation--> planning_rejected
   |
malformed --one repair--> validate
   |
plan_ready -> prepare_workflow -> Phase 1 runtime
```

Every branch has a finite terminal state. There is no open-ended model loop,
model-selected recursion, or model-controlled tool execution.

## Package Boundaries

- `planning.domain` owns strict Pydantic contracts for requests, evidence,
  plans, terminal results, knowledge metadata, and tool policies.
- `planning.ports` defines model, knowledge, cache, usage, and planning
  persistence contracts.
- `planning.application` owns ingestion, retrieval fusion, graph nodes,
  validation, workflow mapping, and orchestration services.
- `planning.adapters` supplies DeepSeek and Mock model gateways, SQLite
  knowledge/planning persistence, FTS5 retrieval, and optional local BGE
  embeddings.
- `workflow` remains the only package allowed to execute tools and mutate
  external state.

The handoff uses the existing `WorkflowDefinition.from_mapping()` boundary, so
model output cannot bypass Phase 1 definition validation.

## Operations Integration

`OperationsService.submit_request()` is the production composition boundary for
the local prototype. A complete API request creates a planning session, indexes
the versioned reference runbooks, retrieves evidence, invokes the configured
model through `BudgetedModelGateway`, validates the resulting plan, maps it to a
workflow definition, and only then starts the sandbox runtime.

The operations API exposes a read-only planning trace containing stage names,
evidence citations, validation status, plan version, knowledge snapshot, model
identity, and usage totals. The console renders this separately from the
authoritative execution audit: planning explains how a proposal was formed;
workflow events prove what was actually allowed and executed.

The default deterministic model keeps the demo offline and reproducible without
short-circuiting the real planning graph. Setting
`CHANGEPILOT_PLANNER_MODE=deepseek` swaps only the model gateway; retrieval,
validation, workflow mapping, and execution authority remain unchanged.

## Knowledge and Evidence

Runbooks carry a stable document ID, immutable version, source, trust level,
policy key, effective date, lifecycle status, and rule digest. Markdown
headings drive deterministic chunks. SQLite stores document versions, chunks,
and an FTS5 trigram index. When the optional BGE adapter is configured, vector
and lexical rankings are combined with reciprocal rank fusion.

Each plan step cites exact evidence references containing document/version,
chunk ID, location, and content digest. Conflicting active policy versions
produce clarification rather than silent selection. A knowledge snapshot digest
binds the plan to the complete indexed state; changed knowledge makes an
already-generated plan stale.

## Model Boundary and Cost Control

`DeepSeekModelGateway` only translates typed model requests and responses.
`BudgetedModelGateway` wraps it with deterministic caching, bounded retries,
usage recording, maximum calls, maximum aggregate tokens, and an optional
estimated-cost ceiling. Prompt, policy, model, tool, and knowledge versions are
part of the reproducibility boundary.

Production configuration should persist cache and usage records through the
SQLite adapters. Default tests use `MockModelGateway`; the live DeepSeek test is
explicitly opt-in and performs at most one small request.

## Security and Authority

Retrieved text is serialized inside the user evidence payload and is always
described by the system prompt as untrusted data. Runbook instructions cannot
change the model role, request secrets, bypass approval, or invoke tools.

The model may propose risk, but it cannot lower risk from either a registered
tool descriptor or a tool policy. High-risk operations require runtime
approval. Side-effecting operations require evidence meeting the configured
trust threshold and, when policy requires it, a registered compensation tool
and intent. Invalid citations, stale evidence, unknown tools, bad arguments,
cycles, missing compensation, and insufficiently trusted evidence are rejected
before runtime handoff.

## Persistence and Recovery

Planning sessions persist the normalized request, attempts, terminal results,
accepted plan versions, prepared workflow binding, model usage, and cache
entries. Clarification answers resume the same session and create another
recorded attempt. Accepted plans are immutable versions.

LangGraph does not own execution checkpoints or side effects. Once prepared,
the Phase 1 runtime owns transactional state transitions, idempotency keys,
approval binding, startup recovery, compensation order, and redacted audit
events. This division keeps probabilistic reasoning separate from reliable
effect execution.

## Current Limits

- The reference adapter uses local SQLite; distributed search and
  multi-tenant isolation are not implemented.
- BGE vectors are scanned in-process and suit a local demonstration, not a
  large production corpus.
- The local Web API and operator UI are single-user demonstrations; there is no
  authentication service, RBAC, secrets manager, or production deployment
  manifest.
- Tool implementations in the reference scenario are deterministic fakes.
  Production tools must provide truthful idempotency and recovery probes.
- The planner proposes one workflow at a time and allows only one schema repair.
