-- rollback: 003_operations.down.sql
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS resource_version integer NOT NULL DEFAULT 1;
ALTER TABLE audiences ADD COLUMN IF NOT EXISTS resource_version integer NOT NULL DEFAULT 1;
ALTER TABLE creatives ADD COLUMN IF NOT EXISTS resource_version integer NOT NULL DEFAULT 1;
ALTER TABLE creatives ADD COLUMN IF NOT EXISTS approval_requested_by varchar(128);
CREATE TABLE IF NOT EXISTS marketing_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id varchar(64) NOT NULL,
  kind varchar(80) NOT NULL,
  aggregate_id uuid NOT NULL,
  state varchar(32) NOT NULL,
  idempotency_key varchar(200) NOT NULL,
  request_fingerprint varchar(64) NOT NULL,
  requested_by varchar(128) NOT NULL,
  correlation_id varchar(128) NOT NULL,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  result_json text,
  error_code varchar(80),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_marketing_operation_idempotency UNIQUE (tenant_id, kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_marketing_operations_tenant_state
  ON marketing_operations(tenant_id, state, created_at);
CREATE INDEX IF NOT EXISTS ix_marketing_operations_aggregate
  ON marketing_operations(tenant_id, aggregate_id, kind);
CREATE INDEX IF NOT EXISTS ix_marketing_operations_correlation
  ON marketing_operations(correlation_id);

CREATE TABLE IF NOT EXISTS marketing_outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id varchar(64) NOT NULL,
  operation_id uuid NOT NULL UNIQUE REFERENCES marketing_operations(id) ON DELETE CASCADE,
  destination varchar(80) NOT NULL,
  event_type varchar(120) NOT NULL,
  payload_json text NOT NULL,
  state varchar(32) NOT NULL DEFAULT 'pending',
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz,
  last_error_code varchar(80),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_marketing_outbox_claim
  ON marketing_outbox(state, next_attempt_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS ix_marketing_outbox_tenant
  ON marketing_outbox(tenant_id, state);

CREATE TABLE IF NOT EXISTS marketing_audit_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id varchar(64) NOT NULL,
  operation_id uuid,
  aggregate_type varchar(80) NOT NULL,
  aggregate_id uuid NOT NULL,
  action varchar(120) NOT NULL,
  outcome varchar(32) NOT NULL,
  actor_id varchar(128) NOT NULL,
  correlation_id varchar(128) NOT NULL,
  detail_json text NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_marketing_audit_tenant_created
  ON marketing_audit_events(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS ix_marketing_audit_operation
  ON marketing_audit_events(operation_id);
CREATE INDEX IF NOT EXISTS ix_marketing_audit_aggregate
  ON marketing_audit_events(tenant_id, aggregate_type, aggregate_id);

CREATE TABLE IF NOT EXISTS marketing_attribution_touches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id varchar(64) NOT NULL,
  event_id varchar(128) NOT NULL,
  lead_id varchar(128) NOT NULL,
  campaign_id uuid,
  channel varchar(64) NOT NULL,
  occurred_at timestamptz NOT NULL,
  metadata_hash varchar(64) NOT NULL,
  idempotency_key varchar(200) NOT NULL,
  request_fingerprint varchar(64) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_attribution_tenant_event UNIQUE (tenant_id, event_id),
  CONSTRAINT uq_attribution_tenant_idempotency UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_attribution_tenant_lead_time
  ON marketing_attribution_touches(tenant_id, lead_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_attribution_tenant_campaign_time
  ON marketing_attribution_touches(tenant_id, campaign_id, occurred_at DESC);
