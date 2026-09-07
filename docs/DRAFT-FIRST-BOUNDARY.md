# Governed draft-first provider boundary — issue #12

This document supersedes the scope limitations of the initial
`DELIVERY-BOUNDARY-HARDENING.md` change. The provider policy, private API, journal,
migration, and production ASGI wrapper described here are now implemented.
Source tests are not provider staging certification or permission to spend.

## Runtime topology and authority

Middleware -> private Marketing commands -> PostgreSQL command journal ->
Marketing provider worker -> server-configured provider account. No n8n8 direct
provider writes, caller credentials, caller URLs, or cross-service database access.

The production image starts `app.asgi:app`. It preserves the existing planning
API while registering `/internal/v1/marketing` and blocking legacy activation,
resume, and globally scoped provider-account reads. Development commands that
start `app.main:app` alone omit the new boundary; do not deploy that entrypoint.

Apply migration `004_provider_boundary.sql` explicitly, after a verified backup;
there are no automatic startup migrations. Run the worker from the same immutable
image, same policy mount, and same database, overriding the image entrypoint:

```sh
python -m app.provider_service
```

Keep the worker separate from the API. Never start this worker with an image
whose code or schema differs from the API. Do not run the destructive disposable
PostgreSQL certification script against a staging or production database.

Configure private ingress/firewall rules so the internal prefix is not publicly
routed. Bind the service to private networking; authenticate requests even on that
network. An `/internal` path and bearer authentication alone do not prove network
isolation. Allow egress only to the configured identity service and the approved
provider. No infrastructure or ingress configuration was applied by this change.

## Identity and configuration

The existing Keycloak issuer, audience, JWKS, and allowed-client checks remain.
`X-Tenant-ID` must match the verified tenant claim. The shared tenant alphabet is
bounded printable ASCII without spaces (1–64 characters); `acme/us` and
`team@example.com` now work through authentication and delivery consistently.

`MARKETING_COMMAND_CLIENT_IDS` is a **separate**, explicit allowlist of Middleware
client IDs. Missing configuration returns 503; unrelated clients return 403.
Issue only these narrowly scoped permissions as needed:

- `marketing.provider.command`: submit provider commands.
- `marketing.provider.approve`: additionally required for approval.
- `marketing.provider.read`: tenant-scoped command, snapshot, and status reads.

Approval subject must differ from preparation subject. The authenticated subject,
not a caller-supplied actor field, is recorded in the journal and audit.

Mount `MARKETING_ACCOUNT_POLICY_FILE` as a regular, read-only JSON file, not a
symlink. Use `config/marketing-account-policy.example.json` only as a sandbox
structure example. Replace example UUIDs with existing, same-tenant audience and
approved creative records. Do not put credentials into this repository.

The policy owns provider account IDs, permitted audiences/creatives, countries,
minimum age, objectives, currency, per-day/total reservation caps, date horizon,
maximum duration, and provider mode. A provider account cannot be assigned to two
tenants or aliases. Unknown accounts and any malformed policy fail closed.

For Meta, select `provider: meta`, `mode: draft_only`, a server-owned numeric account,
its currency, an explicitly verified `graph_version`, required special ad
categories, and a regular `token_file` path supplied by the secret manager.
Graph URLs cannot be supplied in commands or policy. Token files are read at call
time for rotation, bounded, and never placed in URLs or returned to callers.

All image defaults remain:

```text
LIVE_ADVERTISING=false
LIVE_ADVERTISING_ENABLED=false
MARKETING_DRAFT_PROVIDER_WRITES_ENABLED=false
```

The canonical `LIVE_ADVERTISING=false` overrides the historical legacy alias.
Enabling only the separate draft-write flag permits **paused campaign shells** on
an approved draft-only account; this requires staging operator authorization.
The new boundary has no implementation that activates ads or publishes content,
even if either live flag is set. Pause and archive remain safety-reducing actions
and do not depend on a spend-enabling flag.

## API and command flow

| Method | Private path | Purpose |
| --- | --- | --- |
| POST | `/internal/v1/marketing/commands` | Durable canonical command |
| GET | `/internal/v1/marketing/operations/{operation_id}` | Current command outcome |
| GET | `/internal/v1/marketing/campaigns/{campaign_id}` | Current version and saved provider evidence |
| GET | `/internal/v1/marketing/accounts/{account_alias}/connectivity` | Explicit account-bound provider probe |
| GET | `/internal/v1/marketing/status` | Policy/journal readiness and unresolved count |

The original `/health/live`, `/health/ready`, `/version`, and authenticated metrics
remain. Provider `/status` supplements rather than replaces general DB readiness;
query connectivity and campaign snapshots before claiming provider readiness.
Unknown source SHA or image digest is not immutable release evidence.

Every POST requires bearer authentication, `X-Tenant-ID`, `X-Correlation-ID`, and
`Idempotency-Key`. The checked-in schema is
`contracts/marketing.commands.v1.json`; regenerate with
`python scripts/export_marketing_command_contract.py`. The private prefix is not
advertised by the public planning OpenAPI document.

Example shape (substitute existing IDs, account alias, versions and future dates):

```json
{
  "campaign_id": "00000000-0000-4000-8000-000000000001",
  "action": "create_draft",
  "expected_version": 0,
  "draft": {
    "account": "sandbox-example",
    "source_version": 1,
    "audience_id": "00000000-0000-4000-8000-000000000002",
    "creative_id": "00000000-0000-4000-8000-000000000003",
    "starts_at": "2030-01-02T00:00:00Z",
    "ends_at": "2030-01-03T00:00:00Z"
  }
}
```

Those example dates are illustrative, not automatically accepted: the configured
future-date horizon is enforced. Only the initial create contains `draft` and
version zero. All subsequent commands contain the campaign ID, action, and current
**provider plan** version (distinct from source campaign version). Extra fields
and coerced integer versions are rejected. Read the current plan before every
new transition.

A 202 response means the intent is durably journaled, not that a provider write
completed. Its receipt stays identical for the same tenant/actor/key/payload;
a changed payload or actor with that key returns 409. Poll the operation URL for
current status and read the campaign snapshot separately. A newer correlation
header on a replay does not create a new command.

Lifecycle: draft -> approved -> scheduled; pause/archive have explicit guards.
Scheduling is local intent, not activation. `activate` always returns 423.
Approval and scheduling revalidate the saved source/asset versions and content
hashes, approved creative, targeting policy, budget/date constraints, and a saved
paused, zero-spend read-back. Changes to inputs require fresh preparation; this
initial implementation does not patch an existing provider plan in place.

Account-wide reservations are serialized by PostgreSQL advisory locks. Budgets
include all nonarchived plans, not only one campaign. Total reservation rounds up
partial UTC calendar days conservatively. Archive must complete before releasing
its reservation. Provider campaign budget/ad-set publishing is deliberately not
implemented; these caps constrain preparation, not an authorized live spend limit.

## Provider behavior and uncertainty

The local sandbox is deterministic and network-free. Its records are explicitly
`local_simulation`, not sandbox-service or real-provider evidence.

The Meta adapter creates only a paused campaign shell with a unique server-generated
marker. It creates no ad sets, ads, or creatives and never posts ACTIVE. It checks
account identity/currency/status before creation, verifies campaign/account identity
before pause/archive, and performs independent configured-status/spend read-back.
Empty insight data yields `spend_minor: null` (unknown), not zero. Unknown or nonzero
spend prevents approval/scheduling and cannot satisfy zero-spend certification.

A command is marked running and committed **before** the external create. Confirmed
provider IDs are committed before the subsequent read-back. Timeout, crash, failed
read-back, or an expired worker lease quarantines the command as
`reconciliation_required`; the worker does not automatically repeat create.

With a new idempotency key and current plan version, `readback` can reconcile the
same provider ID or locate its exact server marker within the original account.
Lookup is bounded and never follows arbitrary provider-supplied pagination URLs.
No match, multiple matches, or incomplete scans remain unresolved. Even failures
before a possible send are treated conservatively; an operator may need to inspect
the provider and journal. Do not delete journal rows or reuse a campaign ID to
force a retry. Safe pause/archive may reconcile uncertain prior intent and do not
require the original content to remain current. Account reassignment remains denied.

## Certification and rollback checklist

Unit transport tests mock HTTP. PostgreSQL tests use a disposable database and the
local simulator. Neither is evidence from an advertising provider account.

In an independently authorized staging run, record authenticated request/response
and journal evidence for first create, exact replay, changed-payload conflict,
wrong tenant/account, disallowed assets, budget/date/audience/content denials,
concurrent reservations, pause/archive, unknown-outcome reconciliation, provider
identity/status read-back, and authoritative initial/final/intermediate zero spend.
Include correlation IDs, operation IDs, source SHA, immutable image digest, policy
checksum, identity-client configuration checksum, and timestamps. Remove tokens
and secrets from retained evidence. Missing or delayed spend counters block closure.

Before deployment, back up database **including both provider tables** and policy
configuration; verify checksums and a restore rehearsal on an isolated database.
Record existing provider IDs/markers and paused status. Stop the worker before
service rollback, pause/archive any shell created by the candidate, and verify
read-back. Keep the new tables/journal during application rollback so replay and
reconciliation evidence survives. A down migration drops that protection and may
only run after provider reconciliation, backup, and explicit approval.

Promote the exact tested image digest, not a rebuilt tag, through a bounded
no-spend canary with both live flags false. Capture policy and source/image identity
from the actual runtime, including private ingress and Middleware-to-Marketing
identity tests. A separate approved release is necessary to implement and authorize
live advertising; this change does not supply that authority.

Issue #12 remains open until actual provider staging, backup/rollback, ingress,
immutable canary, and zero-spend evidence are attached and reviewed.
