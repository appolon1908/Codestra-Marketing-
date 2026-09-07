-- Run only after stopping both API and workers and backing up the provider journal.
-- Removing this journal while a provider exists destroys duplicate-prevention evidence.
DROP TABLE IF EXISTS marketing_provider_commands;
DROP TABLE IF EXISTS marketing_provider_campaigns;
