DROP TABLE IF EXISTS marketing_attribution_touches;
DROP TABLE IF EXISTS marketing_audit_events;
DROP TABLE IF EXISTS marketing_outbox;
DROP TABLE IF EXISTS marketing_operations;
ALTER TABLE campaigns DROP COLUMN IF EXISTS resource_version;
ALTER TABLE audiences DROP COLUMN IF EXISTS resource_version;
ALTER TABLE creatives DROP COLUMN IF EXISTS resource_version;
ALTER TABLE creatives DROP COLUMN IF EXISTS approval_requested_by;
