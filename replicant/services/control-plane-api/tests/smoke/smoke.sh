#!/bin/sh
# CONTROL component smoke test (M1), against real neighbours where they exist and a contract fake of ReplicantJob:
#   Postgres (PLATFORM image, migrated by PLATFORM's `migrate` image), Temporal CLI dev server, Gitea 28.1 bootstrapped
#   with PLATFORM's gitea-bootstrap v0 script, and the three CONTROL images (control-plane-api:dev + run-grants,
#   github-integration:dev, github-token-broker:dev). The ReplicantJob worker is tests/smoke/stub_worker.py (versioned
#   worker replicant-worker:<build>) unless STUB_WORKER=0 and WORKER's replicant-worker is polling.
#
#   tests/smoke/smoke.sh            # bring up, run, leave running
#   tests/smoke/smoke.sh down       # stop and remove every ctl-* container, volume and network
# Containers are prefixed ctl-. Secrets live in $CTL_HOME/secrets and are never printed.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
CTL_HOME="${CTL_HOME:-${TMPDIR:-/tmp}/ctl-smoke}"; export CTL_HOME
SEC="$CTL_HOME/secrets"
NET=ctl-net
GITEA_IMAGE="${GITEA_IMAGE:-docker.io/gitea/gitea:28.1.0-rootless}"
BUILD="${REPLICANT_WORKER_BUILD_ID:-b20261024-000000000000}"
PY="${PYTHON:-python3}"

if [ "${1:-}" = down ]; then
  pkill -f "stub_worker.py" 2>/dev/null || true
  podman rm -f ctl-cpa ctl-grants ctl-gi ctl-broker ctl-gitea >/dev/null 2>&1 || true
  podman volume rm ctl-gitea-data ctl-gitea-config >/dev/null 2>&1 || true
  "$HERE/env.sh" down
  exit 0
fi

step() { printf '\n== %s\n' "$*"; }
gen() { [ -s "$1" ] || (umask 077; "$PY" -c 'import secrets;print(secrets.token_urlsafe(32),end="")' > "$1"); }

step "1. stores: postgres (migrated) + temporal"
"$HERE/env.sh" up >/dev/null
for n in gitea_admin_password gitea_bot_password gitea_webhook_secret dev_seed_token gi_cp_token broker_gi_token \
         broker_publisher_token broker_mirror_token shim_token; do gen "$SEC/$n"; done
(umask 077
 printf '%s svc_replicant-gateway-shim\n%s svc_github-integration\n' "$(cat "$SEC/shim_token")" "$(cat "$SEC/gi_cp_token")" \
   > "$SEC/control_plane_internal_callers"
 printf '"%s": github-integration\n"%s": publisher-worker\n"%s": repo-mirror\n' "$(cat "$SEC/broker_gi_token")" \
   "$(cat "$SEC/broker_publisher_token")" "$(cat "$SEC/broker_mirror_token")" > "$SEC/broker_caller_tokens")
if [ ! -s "$CTL_HOME/run-grant-jwks.json" ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out "$SEC/run_grant_signing_key" 2>/dev/null
  openssl ec -in "$SEC/run_grant_signing_key" -pubout -outform DER 2>/dev/null | "$PY" -c '
import base64,json,sys
d=sys.stdin.buffer.read()[-65:]; b=lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")
print(json.dumps({"keys":[{"kty":"EC","crv":"P-256","alg":"ES256","use":"sig","kid":"rg-local-1","x":b(d[1:33]),"y":b(d[33:])}]}))' \
    > "$CTL_HOME/run-grant-jwks.json"
fi
chmod 0644 "$SEC"/* "$CTL_HOME/run-grant-jwks.json"   # containers run as non-root uids; the dir stays 0700 for others

step "2. gitea + PLATFORM v0 bootstrap (org acme-payments, repo ledger-api, label replicant, org webhook)"
if ! podman container exists ctl-gitea; then
  podman run -d --name ctl-gitea --network $NET --network-alias git.replicant.localhost -p 127.0.0.1:53300:3300 \
    -e GITEA__server__HTTP_PORT=3300 -e GITEA__server__ROOT_URL=http://git.replicant.localhost:3300/ \
    -e GITEA__server__DOMAIN=git.replicant.localhost -e GITEA__server__DISABLE_SSH=true -e GITEA__database__DB_TYPE=sqlite3 \
    -e GITEA__security__INSTALL_LOCK=true -e GITEA__security__ALLOWED_HOST_LIST=private \
    -e GITEA__webhook__ALLOWED_HOST_LIST=private -e GITEA__service__DISABLE_REGISTRATION=true \
    -e GITEA__service__REQUIRE_SIGNIN_VIEW=true -e GITEA__repository__DEFAULT_BRANCH=main -e GITEA__webhook__DELIVER_TIMEOUT=10 \
    -v ctl-gitea-data:/var/lib/gitea -v ctl-gitea-config:/etc/gitea "$GITEA_IMAGE" >/dev/null
fi
i=0; until podman exec ctl-gitea wget -q -O /dev/null http://127.0.0.1:3300/api/healthz 2>/dev/null; do
  i=$((i+1)); [ $i -gt 60 ] && { echo "gitea not healthy"; exit 1; }; sleep 2; done
# curl resolves *.localhost to loopback itself, so the bootstrap talks to the container name (see INTERFACES-LOG)
podman run --rm --name ctl-gitea-bootstrap --network $NET --entrypoint /bin/sh \
  -e GITEA_ADMIN_PASSWORD_FILE=/run/secrets/gitea_admin_password -e GITEA_BOT_PASSWORD_FILE=/run/secrets/gitea_bot_password \
  -e GITEA_URL=http://ctl-gitea:3300 -e GITEA_WEBHOOK_SECRET_FILE=/run/secrets/gitea_webhook_secret \
  -e GITEA_WEBHOOK_URL=http://github-integration:8080/webhooks/gitea -e GITEA_ORG=acme-payments -e GITEA_SAMPLE_REPOS=ledger-api \
  -v "$SEC:/run/secrets:ro" -v ctl-gitea-data:/var/lib/gitea -v ctl-gitea-config:/etc/gitea \
  -v "$ROOT/dev/compose/config/gitea/bootstrap.sh:/scripts/bootstrap.sh:ro" \
  -v "$ROOT/dev/compose/config/gitea/bootstrap-v0.sh:/scripts/bootstrap-v0.sh:ro" \
  -v "$ROOT/dev/sample-repos:/sample-repos:ro" "$GITEA_IMAGE" /scripts/bootstrap-v0.sh | tail -3

step "3. CONTROL services"
COMMON="-e REPLICANT_ENV=local -e REPLICANT_ORG=local -e REPLICANT_LOG_FORMAT=json -e REPLICANT_TEMPORAL_ADDRESS=ctl-temporal:7233 \
 -e REPLICANT_TEMPORAL_NAMESPACE=replicant-local -e REPLICANT_TEMPORAL_TLS=false -e REPLICANT_WORKER_BUILD_ID=$BUILD \
 -e REPLICANT_WEB_ORIGIN=http://app.replicant.localhost:8400 -v $SEC:/run/secrets:ro"
podman container exists ctl-broker || podman run -d --name ctl-broker --network $NET --network-alias github-token-broker $COMMON \
  -e REPLICANT_HTTP_PORT=8080 -e REPLICANT_BROKER_CALLERS_FILE=/etc/replicant/broker-callers.yaml \
  -e REPLICANT_BROKER_CALLER_TOKENS_FILE=/run/secrets/broker_caller_tokens \
  -e REPLICANT_BROKER_GITEA_BASE_URL=http://git.replicant.localhost:3300 -e REPLICANT_BROKER_GITEA_BOT_USER=replicant-bot \
  -e REPLICANT_BROKER_GITEA_BOT_PASSWORD_FILE=/run/secrets/gitea_bot_password -e REPLICANT_BROKER_GITEA_TOKEN_TTL=3600s \
  -e REPLICANT_DB_DSN=postgresql+psycopg://svc_github_token_broker@ctl-pg:5432/replicant \
  -e REPLICANT_DB_PASSWORD_FILE=/run/secrets/db_svc_github_token_broker \
  -v "$ROOT/dev/compose/config/github-token-broker/broker-callers.yaml:/etc/replicant/broker-callers.yaml:ro" \
  localhost/replicant/github-token-broker:dev >/dev/null
podman container exists ctl-cpa || podman run -d --name ctl-cpa --network $NET --network-alias control-plane-api \
  --network-alias api.replicant.localhost -p 127.0.0.1:58401:8401 $COMMON -e REPLICANT_HTTP_PORT=8401 \
  -e REPLICANT_DB_DSN=postgresql+psycopg://svc_control_plane_api@ctl-pg:5432/replicant \
  -e REPLICANT_DB_PASSWORD_FILE=/run/secrets/db_svc_control_plane_api -e REPLICANT_DEV_SEED_TOKEN_FILE=/run/secrets/dev_seed_token \
  -e REPLICANT_INTERNAL_CALLER_TOKENS_FILE=/run/secrets/control_plane_internal_callers \
  localhost/replicant/control-plane-api:dev >/dev/null
podman container exists ctl-grants || podman run -d --name ctl-grants --network $NET --network-alias grants.replicant.localhost \
  -p 127.0.0.1:58403:8403 $COMMON -e REPLICANT_HTTP_PORT=8403 \
  -e REPLICANT_DB_DSN=postgresql+psycopg://svc_control_plane_api@ctl-pg:5432/replicant \
  -e REPLICANT_DB_PASSWORD_FILE=/run/secrets/db_svc_control_plane_api -e REPLICANT_RUN_GRANT_JWKS_SOURCE=file \
  -e REPLICANT_RUN_GRANT_JWKS_FILE=/etc/replicant/run-grant-jwks.json \
  -v "$CTL_HOME/run-grant-jwks.json:/etc/replicant/run-grant-jwks.json:ro" \
  localhost/replicant/control-plane-api:dev control-plane-api serve-grants --port 8403 >/dev/null
podman container exists ctl-gi || podman run -d --name ctl-gi --network $NET --network-alias github-integration \
  -p 127.0.0.1:58080:8080 $COMMON -e REPLICANT_HTTP_PORT=8080 \
  -e REPLICANT_DB_DSN=postgresql+psycopg://svc_github_integration@ctl-pg:5432/replicant \
  -e REPLICANT_DB_PASSWORD_FILE=/run/secrets/db_svc_github_integration \
  -e REPLICANT_GITEA_WEBHOOK_SECRET_FILE=/run/secrets/gitea_webhook_secret -e REPLICANT_GITEA_BASE_URL=http://git.replicant.localhost:3300 \
  -e REPLICANT_REPLICANT_BOT_LOGINS=replicant-bot -e REPLICANT_CONTROL_PLANE_URL=http://control-plane-api:8401 \
  -e REPLICANT_CONTROL_PLANE_TOKEN_FILE=/run/secrets/gi_cp_token -e REPLICANT_TOKEN_BROKER_URL=http://github-token-broker:8080 \
  -e REPLICANT_TOKEN_BROKER_TOKEN_FILE=/run/secrets/broker_gi_token -e REPLICANT_DEV_SEED_TOKEN_FILE=/run/secrets/dev_seed_token \
  -e REPLICANT_S3_ENDPOINT= localhost/replicant/github-integration:dev >/dev/null
for c in ctl-broker:8080 ctl-cpa:8401 ctl-grants:8403 ctl-gi:8080; do
  n="${c%%:*}"; p="${c##*:}"; path=/readyz; i=0
  until podman exec "$n" python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$p$path',timeout=3)" 2>/dev/null; do
    i=$((i+1)); [ $i -gt 40 ] && { echo "$n not ready"; podman logs --tail 20 "$n"; exit 1; }; sleep 2; done
  echo "$n ready"
done

step "4. ReplicantJob worker (contract fake unless STUB_WORKER=0)"
if [ "${STUB_WORKER:-1}" = 1 ] && ! pgrep -f stub_worker.py >/dev/null; then
  REPLICANT_TEMPORAL_ADDRESS=127.0.0.1:57233 REPLICANT_TEMPORAL_NAMESPACE=replicant-local REPLICANT_WORKER_BUILD_ID=$BUILD \
    nohup "$PY" "$HERE/stub_worker.py" > "$CTL_HOME/stub-worker.log" 2>&1 &
  sleep 3; cat "$CTL_HOME/stub-worker.log"
fi

step "5. seed through the APIs (dev seed token)"
"$PY" "$HERE/smoke.py" seed

step "6. a Gitea issue labelled replicant -> signed webhook -> intake row -> pinned ReplicantJob -> ack comment"
"$PY" "$HERE/smoke.py" run
