## ADDED Requirements

### Requirement: Operations requests use the bounded planning Agent

The system SHALL pass each complete supported operations request through
knowledge retrieval, structured model generation, deterministic validation, and
workflow mapping before execution begins.

#### Scenario: Upgrade request reaches an approval gate

- **WHEN** an operator submits the supported V1-to-V2 upgrade request
- **THEN** the generated plan SHALL cite retrieved runbook evidence
- **AND** the plan SHALL pause before the high-risk migration until approval

#### Scenario: Invalid planning result cannot execute

- **WHEN** planning returns clarification, rejection, or budget exhaustion
- **THEN** the system SHALL NOT prepare or execute a workflow

### Requirement: Planning and execution evidence remain distinguishable

The system SHALL expose a read-only planning trace containing planning stages,
retrieved evidence, validation status, plan version, knowledge snapshot, model
identity, and usage totals.

#### Scenario: Operator inspects a prepared run

- **WHEN** an operator opens a run in the console
- **THEN** the console SHALL show the planning trace separately from the
  authoritative execution audit

### Requirement: Different goals produce different bounded plans

The system SHALL support a read-only readiness goal in addition to the upgrade
goal.

#### Scenario: Readiness assessment performs no change

- **WHEN** the operator requests a readiness assessment
- **THEN** the plan SHALL contain only service inspection, database inspection,
  and precheck steps
- **AND** it SHALL NOT call migration or deployment tools
- **AND** it SHALL finish without approval or compensation
