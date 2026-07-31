# Architecture Overview

ChangePilot separates probabilistic Agent planning from deterministic change
execution. The model interprets intent and proposes a candidate plan grounded
in retrieved knowledge; system code owns authorization, dependencies,
approvals, state transitions, recovery, and audit.

## Layer Responsibilities

| Layer | Responsibility | Documentation |
| --- | --- | --- |
| Agent planning and knowledge | Runbook ingestion, hybrid retrieval, model planning, plan repair, and evidence binding | [Agent Planning and Knowledge](agent-planning-and-knowledge.md) |
| Reliable workflow core | State machines, DAG scheduling, approval barriers, transactions, idempotency, compensation, and recovery | [Reliable Workflow Core](reliable-workflow-core.md) |
| Bounded execution | Execute deployment, migration, and health checks through allowlisted tools and a local sandbox | [Change Execution Sandbox](change-execution-sandbox.md) |
| Operations and evaluation | Present plans, approvals, and evidence to operators and evaluate the Agent closed loop offline | [Operations Console and Evaluation](operations-console-and-evaluation.md) |

## How a Change Flows

```text
natural-language intent
  -> knowledge retrieval and evidence binding
  -> model-generated candidate task graph
  -> deterministic policy validation
  -> human approval for high-risk steps
  -> reliable workflow scheduling
  -> bounded tool execution
  -> verification / compensation / recovery
  -> audit and evaluation
```

## Design Principles

- **The model has no execution authority**: model output is only a candidate
  plan and must pass policy and tool boundaries.
- **Evidence is traceable**: each plan records citations, knowledge versions,
  and model-call metadata.
- **State is recoverable**: runs, steps, attempts, and approvals are persisted
  so the process can decide what to do after restart.
- **Side effects are bounded**: tools use structured arguments, allowlist
  registration, and stable idempotency keys.
- **Failures remain explainable**: original failure, retries, compensation, and
  recovery outcomes stay in the audit chain.
