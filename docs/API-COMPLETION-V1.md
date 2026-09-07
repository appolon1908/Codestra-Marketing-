# Codestra Marketing API Completion V1

This branch completes the governed Marketing control plane on the existing Codestra Marketing repository.

## Canonical surface

- `GET /health/live`, `GET /health/ready`, `GET /version`, private `GET /metrics`
- campaign create/list/detail/update, approval submit/approve/reject, pause/resume
- durable campaign activation request/status
- audience create/list/detail/update
- creative create/list/detail/update and approval lifecycle
- `POST /v1/marketing/attribution/touches` and `GET /v1/marketing/leads/{lead_id}/attribution`
- bounded `GET /v1/marketing/performance` reporting (maximum 93-day window and 500 groups)
- allowlisted Meta read-sync request/status

## Security and durability

Every tenant mutation requires verified Keycloak issuer, audience, client, scope and tenant claims plus `X-Tenant-ID`, `X-Correlation-ID`, and semantic `Idempotency-Key`. Spoofable identity and scope headers are rejected. Budget, currency or approved-content changes invalidate prior approval. Separation of duties prevents self-approval where policy requires it.

Activation and provider synchronization are durable Middleware operations. Marketing never calls Meta or another advertising provider from the request thread and stores no provider credential in campaign payloads.

## Required source evidence

- PostgreSQL migrations and reversible rollback/restore evidence
- tenant/idempotency concurrency and approval-state tests
- committed OpenAPI 3.1 and AsyncAPI 3.0 with runtime parity
- request/auth/idempotency/approval/attribution/provider-sync/reconciliation/safety metrics
- bounded reporting windows and aggregate outputs

## Safety baseline

```text
BUSINESS_WRITES_ENABLED=false
LIVE_ADVERTISING_ENABLED=false
META_READ_SYNC_ENABLED=false
TELEMETRY_EXPORT_ENABLED=false
ADVERTISING_SPEND=0
RUNTIME_DEPLOYED=false
PRODUCTION_CHANGED=false
```
