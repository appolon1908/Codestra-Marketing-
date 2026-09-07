# Backup, restore, and rollback authority

These source controls do not authorize deployment or production delivery.

Before a migration or rollout, an authorized operator runs the recovery backup
with libpq `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and an owner-protected
`PGPASSFILE`, a root-owned mode-0700 backup directory, a verified tmpfs recovery
work root, exact release SHA, exact image digest, an approved OpenPGP recovery
recipient, and a pinned backup-signing fingerprint. It validates a custom-format
dump, encrypts it, destroys the temporary plaintext, and atomically publishes an
exact-signer-authenticated manifest, relocatable checksums, immutable metadata,
and `LAST_SUCCESS`. Database credentials are never passed as process arguments.

Restore verification requires `ALLOW_ISOLATED_RESTORE=true`, a disposable
empty database whose name contains `restore`, and an identity different from the
source backup. The operator supplies the exact expected release SHA and image
digest. The verifier checks the pinned signer and exact manifest membership,
rejects a mismatched tuple before decryption, confines plaintext to verified
tmpfs, restores with `--exit-on-error`, and proves all four marketing tables,
Stage 5 campaign and approval columns, exact provider/idempotency uniqueness
boundaries, and the exact `public.campaign_approvals(campaign_id)` to
`public.campaigns(id)` cascading foreign key before publishing checksum-bearing
evidence.

`check-recovery-freshness.sh` evaluates either backup or restore evidence against
an explicitly supplied RPO/RTO age. Scheduling, off-host replication, approved
RPO/RTO values, current/previous immutable runtime tuples, and a live rehearsal
remain deployment-owner responsibilities.

Application rollback should first redeploy the reviewed previous immutable image
after confirming schema compatibility. Never automatically run down migrations
or restore into production. Database restore requires a separate recovery
decision and must first succeed against an isolated database.
