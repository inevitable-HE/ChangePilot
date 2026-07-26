# ChangePilot Evaluation

- Run: `c4f1a9c4-5fa1-439d-aca9-b8f31c21c7a0`
- Dataset: `changepilot-core@1.0.0`
- Result: `PASS`
- Model: `mock-planner`
- Estimated cost: `0.0`

## Metrics

- `approval_effective_rate`: 1.0
- `compensation_success_rate`: 1.0
- `completion_rate`: 1.0
- `dangerous_action_block_rate`: 1.0
- `evidence_completeness_rate`: 1.0
- `forbidden_action_absence_rate`: 1.0
- `idempotent_replay_rate`: 1.0
- `plan_schema_rate`: 1.0
- `recovery_success_rate`: 1.0
- `required_step_coverage`: 1.0
- `risk_identification_rate`: 1.0
- `average_latency_ms`: 1198.0
- `model_calls`: 3.0
- `total_tokens`: 900.0
- `cache_hits`: 0.0
- `estimated_cost`: 0.0

## Cases

- `upgrade-success` (development): `PASS`
- `health-failure-compensates` (development): `PASS`
- `migration-result-recovery` (holdout): `PASS`
