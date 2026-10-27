#!/bin/sh
# Creates the seven C-ART bucket classes for org=local, env=local, plus the Langfuse bucket.
# Idempotent. Mirrors WS-10's bucket settings that the local S3 server supports (parity.md P-05).
set -eu
export AWS_SECRET_ACCESS_KEY="$(cat "$AWS_SECRET_ACCESS_KEY_FILE")"
S3="aws --endpoint-url $S3_ENDPOINT s3api"
PFX="${REPLICANT_ORG}-replicant-${REPLICANT_ENV}"
CFG=/init/s3-local        # WS-10 generated local configs: <class>.{versioning,encryption,lifecycle,object-lock}.json
# Exactly one S3 server profile is active (s3-seaweedfs or s3-minio); wait for whichever answers.
i=0; until aws --endpoint-url "$S3_ENDPOINT" s3api list-buckets >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -gt 60 ] && { echo "no S3 server at $S3_ENDPOINT"; exit 1; }; sleep 2; done
mk() {
  bucket="$1"; shift
  if $S3 head-bucket --bucket "$bucket" 2>/dev/null; then echo "exists $bucket"; else $S3 create-bucket --bucket "$bucket" "$@"; fi
}
for class in artifacts transcripts session audit memory eval registry; do
  if [ "$class" = audit ]; then mk "$PFX-$class" --object-lock-enabled-for-bucket; else mk "$PFX-$class"; fi
  $S3 put-bucket-versioning --bucket "$PFX-$class" --versioning-configuration "file://$CFG/$class.versioning.json"
  $S3 put-bucket-encryption --bucket "$PFX-$class" --server-side-encryption-configuration "file://$CFG/$class.encryption.json" \
    || echo "encryption config not accepted for $PFX-$class (parity.md P-05)"
  $S3 put-bucket-lifecycle-configuration --bucket "$PFX-$class" --lifecycle-configuration "file://$CFG/$class.lifecycle.json" \
    || echo "lifecycle not accepted for $PFX-$class; the retention sweeper covers it (parity.md P-05)"
done
# audit: Object Lock defaults from WS-10's local file (GOVERNANCE, 1 day). Prod is COMPLIANCE, 730 days (C-ART 2.0.0);
# locally compliance mode would make objects undeletable for 2 years (parity.md P-05, P-37). No identity holds
# s3:BypassGovernanceRetention;
# `replicant dev reset` deletes the store's volume instead of deleting locked objects through the API.
$S3 put-object-lock-configuration --bucket "$PFX-audit" --object-lock-configuration "file://$CFG/audit.object-lock.json"
# ops bucket (C-ART 2.0.0, CCR-09-10): kill-switch mirror (control-plane-api) and the exec revocation feed
# (runner-local / kind runner-api, prefix exec-revocations/local/, 90-day expiry). Versioned, no Object Lock.
# WS-10 ships no local ops config yet, so the settings are inline here.
mk "$PFX-ops"
$S3 put-bucket-versioning --bucket "$PFX-ops" --versioning-configuration Status=Enabled
$S3 put-bucket-lifecycle-configuration --bucket "$PFX-ops" --lifecycle-configuration \
  '{"Rules":[{"ID":"exec-revocations-90d","Status":"Enabled","Filter":{"Prefix":"exec-revocations/"},"Expiration":{"Days":90}}]}' \
  || echo "lifecycle not accepted for $PFX-ops (parity.md P-05)"
# The legal bucket (<org>-replicant-<env>-legal, CCR-10-10) is deliberately not created locally (parity.md P-42).
mk "local-langfuse"
echo "buckets ready"
