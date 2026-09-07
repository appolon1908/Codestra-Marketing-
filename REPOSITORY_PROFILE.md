# Repository Profile — `Codestra-Marketing-`

## Identity

- **Repository:** `appolon1908-hue/Codestra-Marketing-`
- **Category:** Planned platform control plane — marketing
- **Visibility:** `public`
- **Default branch:** `main`
- **Authority:** Protected marketing coordination source; production runtime authority remains unverified
- **Status:** Control-plane source and tests exist. No repository-owned production deployment, immutable release, rollback target, or live safety read-back is certified.

## Intended purpose

Provide a provider-neutral marketing control plane for campaigns, audiences, segments, creatives, approvals, schedules, attribution, budgets, experiments, performance, and governed activation across Codestra-managed businesses.

## Intended ownership

- Marketing campaign and content coordination model
- Audience/segment references, approvals, calendars, attribution, and performance read models
- Governed APIs and operator workflows that call principal runtime systems through Middleware

## Must not own

- Postal/Mautic email runtime, social-provider adapters, CRM business state, or product databases
- Direct privileged writes to providers, Odoo, or n8n
- Provider credentials, customer PII, or production activation by default

## Planned integrations

- Middleware
- `SDK-repository`
- `klyrow.com` and `social.codestra.co`
- Odoo and n8n through governed contracts
- Superset and Grafana for approved analytics/operations views

## Initial milestones

1. Approve authority and resolve overlap with existing marketing/social systems
2. Define domain model, API/events, tenancy, RBAC, consent, audit, idempotency, and read models
3. Build operator UI and provider-neutral adapters behind Middleware
4. Add CI, security, staging, rollback, and explicit production activation gates

## Governance and safety

- No runtime authority exists until architecture and ownership are accepted.
- Never commit provider credentials, customer lists, consent data, campaign payloads, private keys, or secret-bearing evidence.
- All external effects must be capability-gated, idempotent, auditable, and routed through Middleware.
- This document does not create campaigns, contact providers, spend budgets, publish content, or deploy software.

## Account-wide catalog

See `appolon1908-hue/documentaions/REPOSITORY_CATALOG.md`.
