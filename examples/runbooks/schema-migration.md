---
document_id: schema-migration
version: "1.0"
source: examples/runbooks/schema-migration.md
trust_level: authoritative
policy_key: schema-migration
effective_at: "2026-07-24"
status: active
rule_digest: schema-migration-policy-v1
---
# Schema migration

A database schema write is a high-risk operation. It requires explicit human
approval after prechecks and before execution.

# Compensation

Every schema migration must name a registered rollback tool and describe the
intent to restore the previous schema.
