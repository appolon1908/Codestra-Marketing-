#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ "${ALLOW_ISOLATED_RESTORE:-false}" == "true" ]] || { echo "isolated restore requires explicit authorization" >&2; exit 2; }
required=(PGHOST PGPORT PGDATABASE PGUSER PGPASSFILE CODESTRA_MARKETING_BACKUP_DIR CODESTRA_MARKETING_RESTORE_EVIDENCE_DIR CODESTRA_MARKETING_RECOVERY_WORK_ROOT CODESTRA_EXPECTED_RELEASE_SHA CODESTRA_EXPECTED_IMAGE_DIGEST CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT)
for name in "${required[@]}"; do
  [[ -n "${!name:-}" ]] || { echo "required recovery setting is missing: $name" >&2; exit 2; }
done
backup_dir=$CODESTRA_MARKETING_BACKUP_DIR
evidence_root=$CODESTRA_MARKETING_RESTORE_EVIDENCE_DIR
[[ "$CODESTRA_EXPECTED_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "expected release SHA is invalid" >&2; exit 2; }
[[ "$CODESTRA_EXPECTED_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "expected image digest is invalid" >&2; exit 2; }
[[ "$CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT" =~ ^[A-Fa-f0-9]{40}$ ]] || { echo "backup signing fingerprint is invalid" >&2; exit 2; }
CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT=${CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT^^}
[[ ! -L "$PGPASSFILE" && -f "$PGPASSFILE" ]] || { echo "protected PostgreSQL passfile is invalid" >&2; exit 2; }
passfile_mode=$(stat -c '%a' "$PGPASSFILE")
[[ "$passfile_mode" == "400" || "$passfile_mode" == "600" ]] || { echo "unsafe PostgreSQL passfile mode" >&2; exit 2; }
[[ "$(stat -c '%u' "$PGPASSFILE")" == "$(id -u)" ]] || { echo "PostgreSQL passfile owner mismatch" >&2; exit 2; }
[[ ! -L "$CODESTRA_MARKETING_RECOVERY_WORK_ROOT" && -d "$CODESTRA_MARKETING_RECOVERY_WORK_ROOT" ]] || { echo "recovery work root is invalid" >&2; exit 2; }
[[ "$(findmnt -n -o FSTYPE -T "$CODESTRA_MARKETING_RECOVERY_WORK_ROOT")" == tmpfs ]] || { echo "plaintext recovery work requires tmpfs" >&2; exit 2; }
for file in database.dump.gpg METADATA SIGNED-MANIFEST SIGNED-MANIFEST.sig SHA256SUMS; do
  [[ -f "$backup_dir/$file" ]] || { echo "backup artifact is missing: $file" >&2; exit 2; }
done
(cd "$backup_dir" && [[ "$(awk '{print $2}' SIGNED-MANIFEST)" == $'database.dump.gpg\nMETADATA' ]]) || { echo "signed manifest entries are invalid" >&2; exit 2; }
(cd "$backup_dir" && [[ "$(awk '{print $2}' SHA256SUMS)" == $'database.dump.gpg\nMETADATA\nSIGNED-MANIFEST\nSIGNED-MANIFEST.sig' ]]) || { echo "checksum manifest entries are invalid" >&2; exit 2; }
(cd "$backup_dir" && sha256sum -c SHA256SUMS)
signature_status=$(gpg --batch --status-fd=1 --verify "$backup_dir/SIGNED-MANIFEST.sig" "$backup_dir/SIGNED-MANIFEST" 2>/dev/null)
valid_fingerprint=$(awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" {print toupper($3)}' <<<"$signature_status")
[[ "$valid_fingerprint" == "$CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT" ]] || { echo "backup signature verification failed" >&2; exit 2; }
(cd "$backup_dir" && sha256sum -c SIGNED-MANIFEST)
metadata_value() { sed -n "s/^$1=//p" "$backup_dir/METADATA"; }
[[ "$(metadata_value SCHEMA)" == "codestra-marketing-backup.v1" ]] || { echo "unsupported backup schema" >&2; exit 2; }
source_database=$(metadata_value DATABASE)
release_sha=$(metadata_value RELEASE_SHA)
image_digest=$(metadata_value IMAGE_DIGEST)
signing_fingerprint=$(metadata_value SIGNING_FINGERPRINT)
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid backup release SHA" >&2; exit 2; }
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "invalid backup image digest" >&2; exit 2; }
[[ "$release_sha" == "$CODESTRA_EXPECTED_RELEASE_SHA" ]] || { echo "backup release SHA mismatch" >&2; exit 2; }
[[ "$image_digest" == "$CODESTRA_EXPECTED_IMAGE_DIGEST" ]] || { echo "backup image digest mismatch" >&2; exit 2; }
[[ "$signing_fingerprint" == "$CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT" ]] || { echo "backup signing identity mismatch" >&2; exit 2; }
target_database=$(psql -XAtq -v ON_ERROR_STOP=1 -c 'select current_database()')
[[ "$target_database" != "$source_database" ]] || { echo "refusing restore into source database identity" >&2; exit 2; }
[[ "$target_database" =~ (^|_)restore(_|$) ]] || { echo "restore target is not explicitly isolated" >&2; exit 2; }
existing_user_objects=$(psql -XAtq -v ON_ERROR_STOP=1 <<'SQL'
with user_namespaces as (select oid, nspname from pg_namespace where nspname <> 'information_schema' and nspname !~ '^pg_'),
object_counts as (
  select count(*)::bigint amount from user_namespaces where nspname <> 'public'
  union all select count(*) from pg_class c join user_namespaces n on n.oid=c.relnamespace
  union all select count(*) from pg_proc p join user_namespaces n on n.oid=p.pronamespace
  union all select count(*) from pg_type t join user_namespaces n on n.oid=t.typnamespace
  union all select count(*) from pg_extension where extname <> 'plpgsql'
) select coalesce(sum(amount), 0) from object_counts;
SQL
)
[[ "$existing_user_objects" == 0 ]] || { echo "isolated restore database contains user objects" >&2; exit 2; }

work=$(mktemp -d "$CODESTRA_MARKETING_RECOVERY_WORK_ROOT/restore.XXXXXX")
cleanup() { find "$work" -mindepth 1 -delete 2>/dev/null || true; rmdir "$work" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
gpg --batch --quiet --decrypt --output "$work/database.dump" "$backup_dir/database.dump.gpg"
pg_restore --list "$work/database.dump" >/dev/null
pg_restore --exit-on-error --clean --if-exists --no-owner --no-acl --dbname="$PGDATABASE" "$work/database.dump"
table_count=$(psql -XAtq -v ON_ERROR_STOP=1 <<'SQL'
select count(*) from information_schema.tables where table_schema='public'
and table_name in ('campaigns','campaign_approvals','audiences','creatives');
SQL
)
[[ "$table_count" == "4" ]] || { echo "required table verification failed" >&2; exit 1; }
column_count=$(psql -XAtq -v ON_ERROR_STOP=1 <<'SQL'
select count(*) from information_schema.columns where table_schema='public'
and ((table_name='campaigns' and column_name in ('tenant_id','state','provider','provider_campaign_id','idempotency_key','request_fingerprint','daily_budget_minor','currency'))
  or (table_name='campaign_approvals' and column_name in ('tenant_id','campaign_id','state','decided_at')));
SQL
)
[[ "$column_count" == "12" ]] || { echo "required column verification failed" >&2; exit 1; }
unique_index_count=$(psql -XAtq -v ON_ERROR_STOP=1 <<'SQL'
with expected(table_name, columns, predicate) as (values
  ('campaigns', array['tenant_id','provider','provider_campaign_id']::text[], null::text),
  ('campaigns', array['tenant_id','idempotency_key']::text[], null::text),
  ('audiences', array['tenant_id','idempotency_key']::text[], null::text),
  ('creatives', array['tenant_id','idempotency_key']::text[], null::text)
)
select count(*) from expected e where exists (
  select 1 from pg_index i
  join pg_class tbl on tbl.oid=i.indrelid
  join pg_namespace ns on ns.oid=tbl.relnamespace
  where ns.nspname='public' and tbl.relname=e.table_name
    and i.indisunique and i.indisvalid and i.indisready
    and ((e.predicate is null and i.indpred is null)
         or pg_get_expr(i.indpred, i.indrelid)=e.predicate)
    and i.indnkeyatts=cardinality(e.columns)
    and (select array_agg(att.attname::text order by key.ordinality)
         from unnest(i.indkey::smallint[]) with ordinality as key(attnum, ordinality)
         join pg_attribute att on att.attrelid=i.indrelid and att.attnum=key.attnum
         where key.ordinality <= i.indnkeyatts) = e.columns
);
SQL
)
[[ "$unique_index_count" == "4" ]] || { echo "tenant safety constraint verification failed" >&2; exit 1; }
foreign_key_count=$(psql -XAtq -v ON_ERROR_STOP=1 <<'SQL'
select count(*) from pg_constraint fk
join pg_class child on child.oid=fk.conrelid
join pg_class parent on parent.oid=fk.confrelid
join pg_namespace child_ns on child_ns.oid=child.relnamespace
join pg_namespace parent_ns on parent_ns.oid=parent.relnamespace
where child_ns.nspname='public' and child.relname='campaign_approvals'
  and parent_ns.nspname='public'
  and parent.relname='campaigns' and fk.contype='f' and fk.confdeltype='c'
  and (select array_agg(att.attname::text order by key.ordinality)
       from unnest(fk.conkey::smallint[]) with ordinality as key(attnum, ordinality)
       join pg_attribute att on att.attrelid=fk.conrelid and att.attnum=key.attnum)
      = array['campaign_id']::text[]
  and (select array_agg(att.attname::text order by key.ordinality)
       from unnest(fk.confkey::smallint[]) with ordinality as key(attnum, ordinality)
       join pg_attribute att on att.attrelid=fk.confrelid and att.attnum=key.attnum)
      = array['id']::text[];
SQL
)
[[ "$foreign_key_count" == "1" ]] || { echo "campaign approval foreign-key verification failed" >&2; exit 1; }

install -d -m 0700 "$evidence_root"
exec 8>"$evidence_root/.restore.lock"
flock -n 8 || { echo "another restore verification is publishing evidence" >&2; exit 3; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
result_name="RESTORE-RESULT-$stamp"
result="$evidence_root/.$result_name"
[[ ! -e "$result" && ! -e "$evidence_root/$result_name" ]] || { echo "restore evidence stamp collision" >&2; exit 3; }
cat >"$result" <<EOF
SCHEMA=codestra-marketing-restore-result.v1
STAMP=$stamp
BACKUP_STAMP=$(metadata_value STAMP)
RELEASE_SHA=$release_sha
IMAGE_DIGEST=$image_digest
TARGET_CLASS=ISOLATED
TABLE_VERIFICATION=PASS
COLUMN_VERIFICATION=PASS
TENANT_CONSTRAINTS=PASS
APPROVAL_FOREIGN_KEY=PASS
RESTORE=PASS
EOF
chmod 0600 "$result"
sync "$result"
mv "$result" "$evidence_root/$result_name"
(cd "$evidence_root" && sha256sum "$result_name" >".$result_name.sha256")
chmod 0600 "$evidence_root/.$result_name.sha256"
sync "$evidence_root/.$result_name.sha256"
mv "$evidence_root/.$result_name.sha256" "$evidence_root/$result_name.sha256"
printf '%s\n' "$stamp" >"$evidence_root/.LAST_SUCCESS-$stamp"
chmod 0600 "$evidence_root/.LAST_SUCCESS-$stamp"
sync "$evidence_root/.LAST_SUCCESS-$stamp"
mv "$evidence_root/.LAST_SUCCESS-$stamp" "$evidence_root/LAST_SUCCESS"
sync -d "$evidence_root"
echo "restore=PASS target_class=ISOLATED release_sha=$release_sha image_digest=$image_digest"
