#!/usr/bin/env bash
set -euo pipefail

root=${CODESTRA_RECOVERY_ROOT:?recovery root is required}
max_age=${CODESTRA_RECOVERY_MAX_AGE_SECONDS:?maximum age is required}
signer=${CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT:-}
[[ "$max_age" =~ ^[1-9][0-9]*$ ]] || { echo "maximum age must be a positive integer" >&2; exit 2; }
[[ -f "$root/LAST_SUCCESS" ]] || { echo "recovery success marker is missing" >&2; exit 1; }
stamp=$(tr -d '\r\n' <"$root/LAST_SUCCESS")
[[ "$stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { echo "invalid recovery success marker" >&2; exit 1; }
artifact="$root/$stamp"
if [[ -d "$artifact" ]]; then
  [[ "$signer" =~ ^[A-Fa-f0-9]{40}$ ]] || { echo "backup signing fingerprint is invalid" >&2; exit 2; }
  signer=${signer^^}
  for file in database.dump.gpg METADATA SIGNED-MANIFEST SIGNED-MANIFEST.sig SHA256SUMS; do
    [[ -f "$artifact/$file" ]] || { echo "backup evidence is incomplete" >&2; exit 1; }
  done
  manifest_entries=$(awk '{print $2}' "$artifact/SIGNED-MANIFEST")
  [[ "$manifest_entries" == $'database.dump.gpg\nMETADATA' ]] || { echo "signed manifest entries are invalid" >&2; exit 1; }
  checksum_entries=$(awk '{print $2}' "$artifact/SHA256SUMS")
  [[ "$checksum_entries" == $'database.dump.gpg\nMETADATA\nSIGNED-MANIFEST\nSIGNED-MANIFEST.sig' ]] || { echo "checksum manifest entries are invalid" >&2; exit 1; }
  (cd "$artifact" && sha256sum -c SHA256SUMS >/dev/null)
  signature_status=$(gpg --batch --status-fd=1 --verify "$artifact/SIGNED-MANIFEST.sig" "$artifact/SIGNED-MANIFEST" 2>/dev/null)
  valid_fingerprint=$(awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" {print toupper($3)}' <<<"$signature_status")
  [[ "$valid_fingerprint" == "$signer" ]] || { echo "backup signature verification failed" >&2; exit 1; }
  (cd "$artifact" && sha256sum -c SIGNED-MANIFEST >/dev/null)
  [[ "$(sed -n 's/^STAMP=//p' "$artifact/METADATA")" == "$stamp" ]] || { echo "backup marker does not match verified metadata" >&2; exit 1; }
else
  result_name="RESTORE-RESULT-$stamp"
  artifact="$root/$result_name"
  [[ -f "$artifact" && -f "$artifact.sha256" ]] || { echo "restore evidence is incomplete" >&2; exit 1; }
  [[ "$(awk '{print $2}' "$artifact.sha256")" == "$result_name" ]] || { echo "restore checksum entry is invalid" >&2; exit 1; }
  (cd "$root" && sha256sum -c "$result_name.sha256" >/dev/null)
  [[ "$(sed -n 's/^STAMP=//p' "$artifact")" == "$stamp" ]] || { echo "restore marker does not match verified result" >&2; exit 1; }
  grep -qx 'RESTORE=PASS' "$artifact" || { echo "restore result is not successful" >&2; exit 1; }
fi
stamp_iso="${stamp:0:4}-${stamp:4:2}-${stamp:6:2}T${stamp:9:2}:${stamp:11:2}:${stamp:13:2}Z"
stamp_epoch=$(date -u -d "$stamp_iso" +%s)
now_epoch=$(date -u +%s)
age=$((now_epoch - stamp_epoch))
(( age >= -300 )) || { echo "recovery marker is unreasonably in the future" >&2; exit 1; }
(( age <= max_age )) || { echo "recovery evidence is stale age_seconds=$age" >&2; exit 1; }
echo "recovery_freshness=PASS age_seconds=$age max_age_seconds=$max_age"
