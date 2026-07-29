# ChangePilot Documentation

ChangePilot is a bounded AI Agent for high-risk engineering changes. It turns
natural-language change intent into an auditable task graph, executes under
knowledge evidence, deterministic policy, and human approval, and supports
compensation, recovery, and traceability after failures or process interruption.

## Where to Start

- **First visit**: read the [User Guide](user-guide.md) to understand the closed
  loop through an order-service upgrade.
- **Run the local demo**: follow the five-minute success demo to start the API
  and operations console.
- **Understand the Agent boundary**: read
  [Agent Planning and Knowledge](architecture/agent-planning-and-knowledge.md)
  to see how retrieval, model planning, policy validation, and workflow handoff
  cooperate.
- **Understand reliability**: start from the
  [Architecture Overview](architecture/index.md) and continue into workflow,
  sandbox execution, recovery, and evaluation.

## Core Closed Loop

1. A user submits a service upgrade and database schema change intent.
2. The Agent retrieves versioned Runbooks and binds traceable evidence.
3. A model proposes a task graph; deterministic policy validates dependencies,
   tool authority, and risk.
4. High-risk steps stop for human approval bound to the exact plan version.
5. The reliable workflow runtime invokes local tools and persists state,
   attempts, and audit events.
6. Failures trigger reverse-dependency compensation; interrupted processes
   recover from durable state.
7. The operations console presents execution evidence, while the evaluation
   suite tests planning quality and closed-loop reliability.

## Current Boundary

The current release is a runnable engineering prototype. A local sandbox
simulates service deployment, schema migration, and health checks without
connecting to production systems. The model can propose structured plans, but
cannot bypass the tool allowlist, deterministic validation, or human approval
to execute high-risk actions.
