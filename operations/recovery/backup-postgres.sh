#!/usr/bin/env bash
set -euo pipefail
umask 077

required=(PGHOST PGPORT PGDATABASE PGUSER PGPASSFILE CODESTRA_MARKETING_BACKUP_ROOT CODESTRA_MARKETING_RECOVERY_WORK_ROOT CODESTRA_RELEASE_SHA CODESTRA_IMAGE_DIGEST CODESTRA_BACKUP_GPG_RECIPIENT CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT)
for name in "${required[@]}"; do
  [[ -n "${!name:-}" ]] || { echo "required recovery setting is missing: $name" >&2; exit 2; }
done
[[ "$CODESTRA_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "release SHA is not immutable" >&2; exit 2; }
[[ "$CODESTRA_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "image digest is not immutable" >&2; exit 2; }
[[ "$CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT" =~ ^[A-Fa-f0-9]{40}$ ]] || { echo "backup signing fingerprint is invalid" >&2; exit 2; }
CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT=${CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT^^}
gpg --batch --list-secret-keys "$CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT" >/dev/null 2>&1 || { echo "authorized backup signing key is unavailable" >&2; exit 2; }
[[ ! -L "$PGPASSFILE" && -f "$PGPASSFILE" ]] || { echo "protected PostgreSQL passfile is invalid" >&2; exit 2; }
passfile_mode=$(stat -c '%a' "$PGPASSFILE")
[[ "$passfile_mode" == "400" || "$passfile_mode" == "600" ]] || { echo "unsafe PostgreSQL passfile mode" >&2; exit 2; }
[[ "$(stat -c '%u' "$PGPASSFILE")" == "$(id -u)" ]] || { echo "PostgreSQL passfile owner mismatch" >&2; exit 2; }
[[ ! -L "$CODESTRA_MARKETING_RECOVERY_WORK_ROOT" && -d "$CODESTRA_MARKETING_RECOVERY_WORK_ROOT" ]] || { echo "recovery work root is invalid" >&2; exit 2; }
[[ "$(findmnt -n -o FSTYPE -T "$CODESTRA_MARKETING_RECOVERY_WORK_ROOT")" == tmpfs ]] || { echo "plaintext recovery work requires tmpfs" >&2; exit 2; }

backup_root=$CODESTRA_MARKETING_BACKUP_ROOT
install -d -m 0700 "$backup_root"
exec 9>"$backup_root/.backup.lock"
flock -n 9 || { echo "another backup is active" >&2; exit 3; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
destination="$backup_root/$stamp"
[[ ! -e "$destination" ]] || { echo "backup stamp collision" >&2; exit 3; }
work=$(mktemp -d "$CODESTRA_MARKETING_RECOVERY_WORK_ROOT/${stamp}.XXXXXX")
publish=$(mktemp -d "$backup_root/.${stamp}.publishing.XXXXXX")
cleanup() {
  find "$work" -mindepth 1 -delete 2>/dev/null || true; rmdir "$work" 2>/dev/null || true
  find "$publish" -mindepth 1 -delete 2>/dev/null || true; rmdir "$publish" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

database_name=$(psql -XAtq -v ON_ERROR_STOP=1 -c 'select current_database()')
[[ "$database_name" == "codestra_marketing" || "$database_name" == "codestra_marketing_stage5" ]] || {
  echo "refusing unexpected database identity" >&2
  exit 2
}
pg_dump --format=custom --no-owner --no-acl --file="$work/database.dump"
pg_restore --list "$work/database.dump" >/dev/null
gpg --batch --yes --trust-model always --recipient "$CODESTRA_BACKUP_GPG_RECIPIENT" \
  --encrypt --output "$work/database.dump.gpg" "$work/database.dump"
shred -u "$work/database.dump"
mv "$work/database.dump.gpg" "$publish/database.dump.gpg"
cat >"$publish/METADATA" <<EOF
SCHEMA=codestra-marketing-backup.v1
STAMP=$stamp
DATABASE=$database_name
RELEASE_SHA=$CODESTRA_RELEASE_SHA
IMAGE_DIGEST=$CODESTRA_IMAGE_DIGEST
SIGNING_FINGERPRINT=$CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT
ENCRYPTION=OPENPGP
EOF
(cd "$publish" && sha256sum database.dump.gpg METADATA >SIGNED-MANIFEST)
gpg --batch --yes --local-user "$CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT" --detach-sign --output "$publish/SIGNED-MANIFEST.sig" "$publish/SIGNED-MANIFEST"
(cd "$publish" && sha256sum database.dump.gpg METADATA SIGNED-MANIFEST SIGNED-MANIFEST.sig >SHA256SUMS)
chmod 0600 "$publish"/*
sync "$publish/database.dump.gpg" "$publish/METADATA" "$publish/SIGNED-MANIFEST" "$publish/SIGNED-MANIFEST.sig" "$publish/SHA256SUMS"
sync -d "$publish"
mv "$publish" "$destination"
rmdir "$work"
trap - EXIT INT TERM
sync -d "$destination"
marker="$backup_root/.LAST_SUCCESS-$stamp"
printf '%s\n' "$stamp" >"$marker"
chmod 0600 "$marker"
sync "$marker"
mv "$marker" "$backup_root/LAST_SUCCESS"
sync -d "$backup_root"
echo "backup=PASS stamp=$stamp release_sha=$CODESTRA_RELEASE_SHA image_digest=$CODESTRA_IMAGE_DIGEST"

