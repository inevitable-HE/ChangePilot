---
document_id: rollback-policy
version: "1.0"
source: examples/runbooks/rollback-policy.md
trust_level: internal
policy_key: rollback-policy
effective_at: "2026-07-24"
status: active
rule_digest: rollback-policy-v1
---
# Rollback order

If deployment validation fails, restore the previous service version before
rolling back the database schema. Preserve the original error for audit.
