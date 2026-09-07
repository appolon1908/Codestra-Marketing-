# Codestra Marketing

Marketing control-plane repository for campaign strategy, audiences, creatives, budgets, attribution, approvals, optimization, and advertising-provider integrations.

## Current phase
Architecture and documentation bootstrap only. Live campaign activation and autonomous spend remain disabled until explicit production approval controls exist.

## Runtime authority

`config/production-runtime-authority.json` records the observed off-host DNS/TLS
state without treating it as deployment proof. The record remains
`UNVERIFIED_OFF_HOST`, `production_ready=false`, and explicitly forbids both
deployment authorization and production business writes until every runtime,
provenance, safety, gateway, and rollback gate is independently evidenced.
