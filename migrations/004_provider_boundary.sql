CREATE TABLE IF NOT EXISTS marketing_provider_campaigns (
    campaign_id uuid PRIMARY KEY REFERENCES campaigns(id) ON DELETE RESTRICT,
    tenant_id varchar(64) NOT NULL,
    account_alias varchar(64) NOT NULL,
    binding_hash varchar(64) NOT NULL,
    prepared_json text NOT NULL,
    marker varchar(80) NOT NULL UNIQUE,
    state varchar(24) NOT NULL DEFAULT 'draft'
        CHECK (state IN ('draft','approved','scheduled','active','paused','archived')),
    resource_version integer NOT NULL DEFAULT 1 CHECK (resource_version > 0),
    prepared_by varchar(128) NOT NULL,
    approved_by varchar(128),
    provider_id varchar(128),
    snapshot_json text,
    pending_command_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_provider_campaign_tenant ON marketing_provider_campaigns(tenant_id);
CREATE TABLE IF NOT EXISTS marketing_provider_commands (
    id uuid PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    campaign_id uuid NOT NULL,
    action varchar(24) NOT NULL CHECK (action IN ('create_draft','approve','schedule','pause','archive','readback')),
    target_state varchar(24) NOT NULL CHECK (target_state IN ('draft','approved','scheduled','active','paused','archived')),
    expected_version integer NOT NULL CHECK (expected_version > 0),
    idempotency_key varchar(200) NOT NULL,
    fingerprint varchar(64) NOT NULL,
    requested_by varchar(128) NOT NULL,
    correlation_id varchar(128) NOT NULL,
    state varchar(32) NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','running','completed','reconciliation_required','reconciled')),
    receipt_json text NOT NULL,
    error_code varchar(80),
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_provider_command_key UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_provider_commands_tenant ON marketing_provider_commands(tenant_id);
CREATE INDEX IF NOT EXISTS ix_provider_commands_campaign ON marketing_provider_commands(campaign_id);
CREATE INDEX IF NOT EXISTS ix_provider_commands_state ON marketing_provider_commands(state, created_at);
