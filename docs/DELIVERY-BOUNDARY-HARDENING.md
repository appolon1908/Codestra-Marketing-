# Marketing delivery boundary hardening

## Scope and safety status

Related issue: #12. This change hardens the existing Marketing-to-Middleware
source boundary. It does **not** certify a provider, deploy a runtime, authorize
spend/publication, or complete issue #12. `LIVE_ADVERTISING_ENABLED` and
`META_READ_SYNC_ENABLED` remain disabled by default. No deployment manifests,
credentials, database schemas, public routes, or public OpenAPI documents change.

Marketing owns intent and approvals. Middleware remains the privileged
cross-system write boundary; no Odoo, n8n, advertising-provider or arbitrary-URL
bypass is introduced.

## Closed command contract

`app/delivery_contract.py` is shared by the worker and HTTP client. It accepts
exactly `operation_id`, `campaign_id`, `action`, `expected_state`,
`expected_version`, `tenant_id`, and `correlation_id`.

- Operation/campaign identifiers must be UUIDs; versions must be actual positive
  integers within the PostgreSQL integer range, not booleans, strings or floats.
- Tenant and correlation identifiers are bounded ASCII identifier tokens. Extra
  fields, provider account IDs, credentials, URLs and arbitrary options fail
  validation rather than being silently ignored or forwarded.
- Activation and resume reference `approved`; ordinary pause references `paused`.
  The existing approval-invalidation stop remains a pause referencing the prior
  approved version while the local campaign is draft.
- Dispatch binds the command to the stored operation ID, campaign ID, tenant,
  correlation ID, operation kind, outbox destination and exact event type.

Existing receiver paths remain `/api/v1/control/marketing/campaign-activations`
and `/api/v1/control/marketing/campaign-transitions`. Unknown actions never fall
through to the transition endpoint.

## Transport and receipt handling

`MIDDLEWARE_BASE_URL` must be an origin: HTTPS, or HTTP on exact loopback for local
use. Prefix paths, credentials, query strings, fragments, control characters,
backslashes, malformed ports and malformed IPv6 are rejected. A trailing slash
is supported. Private routing and egress allowlists are still deployment duties;
HTTPS alone does not prove that an endpoint is private or authorized.

The client explicitly refuses redirects and ignores ambient HTTP proxy settings.
The token is reread per delivery to support rotation; it must be a bounded regular
file, not a symlink or FIFO. Missing, unreadable, empty, oversized and malformed
tokens produce controlled errors. Tokens are sent only in Authorization headers.

Every retry preserves the command's operation ID as `Idempotency-Key`, tenant,
correlation and normalized body. HTTP 408/429/5xx, transport failures and malformed
acknowledgements retain the existing bounded retry/reconciliation behavior. Other
HTTP failures, including redirects and conflicts, are not reported as success.

A receipt needs a nonempty bounded operation identifier and an explicitly
recognized state (`accepted`, `pending`, `queued`, `processing`, `completed`, or
`succeeded`). A downstream identifier may differ from Marketing's identifier.
Optional tenant/campaign/correlation echoes must match. Only operation ID and
state are retained; arbitrary response content is not copied into the ledger.

A Middleware acknowledgement is **not** evidence that a provider campaign was
created, paused, published or charged. Receiver compatibility and provider
read-back still require end-to-end certification.

## Worker behavior

Malformed stored JSON enters the normal dead-letter/audit path instead of
terminating the worker after its lease is committed. The spend switch is checked
again immediately before dispatch, after claiming and validating the campaign.
The existing version check, campaign lock and completion/failure attempt fence
remain. Late success/failure cannot resurrect retired operations, and late failure
cannot schedule a retry for a superseded operation. Audit action labels use a
bounded allowlist rather than arbitrary payload text.

## Validation

Run the normal repository checks:

```sh
python -m compileall -q app scripts tests
pytest -q -m 'not postgres'
python scripts/validate_api_contract.py
# With the repository's disposable PostgreSQL service and migrations:
pytest -q -m postgres
```

The focused regressions are `tests/test_middleware_delivery_contract.py` and
`tests/test_outbox_delivery_guards.py`. Transport calls and worker database I/O in
these new unit tests are mocked. They prove validation and dispatch decisions,
not PostgreSQL concurrency or provider integration. The existing required
PostgreSQL and container checks must still pass on the final PR head.

## Remaining issue #12 gates

Issue #12 must remain open until tenant/account/provider policy, budget ceilings,
date bounds, audience/content policy, draft/sandbox provider creation, full
scheduled/active/archived lifecycle handling and authoritative spend counters are
implemented and independently certified. The current source still does not
establish those guarantees.

Staging must prove first create, exact replay, changed-payload conflict, wrong
account/tenant denial, spend-cap denial, rollback/pause, provider read-back and
zero live spend. Backup/restore, immutable image promotion, no-spend canary and
separate human activation approval remain release gates. Do not enable delivery
or relax branch protection merely because these source tests pass.
