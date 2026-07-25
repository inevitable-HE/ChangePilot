---
document_id: order-service-upgrade
version: "1.0"
source: examples/runbooks/order-service-upgrade.md
trust_level: authoritative
policy_key: order-service-upgrade
effective_at: "2026-07-24"
status: active
rule_digest: order-upgrade-policy-v1
---
# Order service upgrade

Inspect the current service and database state before making changes. Run the
combined precheck only after both inspections succeed.

# Deployment

Deploy the target service version only after the required schema migration is
complete. Verify the health endpoint and then run the smoke test.
