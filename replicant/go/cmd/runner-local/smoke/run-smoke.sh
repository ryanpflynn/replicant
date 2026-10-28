#!/bin/sh
# runner-local component smoke + Runner contract cases (test-strategy.md §5.3, M1 subset) on an isolated
# Podman network with `run-` prefixed containers: SeaweedFS, the Temporal dev server, the real gateway shim
# (script backend), repo-mirror, runner-local, and the smoke driver (contract fakes for grants/Gitea/broker).
#
#   go/cmd/runner-local/smoke/run-smoke.sh [--build] [--keep] [driver args, e.g. --mode smoke | --only SMOKE-IMPL]
#
# Secrets are generated under $SMOKE_HOME (default ~/.cache/replicant-runner-smoke, inside the podman machine share).
set -eu
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
SMOKE_HOME="${SMOKE_HOME:-$HOME/.cache/replicant-runner-smoke}"
SOCK="${REPLICANT_CONTAINER_SOCKET:-$(podman info --format '{{.Host.RemoteSocket.Path}}' | sed 's#^unix://##')}"
NET=run-smoke
BUILD=0; KEEP=0
while [ $# -gt 0 ]; do case "$1" in --build) BUILD=1; shift;; --keep) KEEP=1; shift;; *) break;; esac; done

if [ "$BUILD" = 1 ]; then
  podman build -q -t localhost/replicant/runner-local:dev -f "$ROOT/go/cmd/runner-local/Dockerfile" "$ROOT"
  podman build -q -t localhost/replicant/repo-mirror:dev -f "$ROOT/go/cmd/repo-mirror/Dockerfile" "$ROOT"
  podman build -q -t localhost/replicant/sandbox-sidecars:dev --target sidecars -f "$ROOT/go/cmd/sandbox-sidecars/Dockerfile" "$ROOT"
  podman build -q -t localhost/replicant/pause:3.10 --target pause -f "$ROOT/go/cmd/sandbox-sidecars/Dockerfile" "$ROOT"
  podman build -q -t localhost/replicant/cli-claude-code:dev -f "$ROOT/images/cli-claude-code/Dockerfile" "$ROOT"
  podman build -q -t localhost/replicant/env-go-service:dev -f "$ROOT/images/env-go-service/Dockerfile" "$ROOT"
fi

cleanup() {
  for n in $(podman ps -a --format '{{.Names}}' | grep -E '^(run-(s3|temporal|shim|mirror|runner|driver)|ar-)'); do podman rm -f -v "$n" >/dev/null 2>&1 || true; done
  for v in $(podman volume ls --format '{{.Name}}' | grep -E '^ar-'); do podman volume rm -f "$v" >/dev/null 2>&1 || true; done
  podman volume rm -f run-runner-state run-mirror-cache >/dev/null 2>&1 || true
  podman network rm -f "$NET" >/dev/null 2>&1 || true
}
cleanup
[ "$KEEP" = 1 ] || trap cleanup EXIT

mkdir -p "$SMOKE_HOME/secrets" "$SMOKE_HOME/scripts"
S="$SMOKE_HOME/secrets"
rnd() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-43; }
[ -f "$S/run_token_signing_key" ] || openssl ecparam -name prime256v1 -genkey -noout 2>/dev/null | openssl pkcs8 -topk8 -nocrypt -out "$S/run_token_signing_key"
for t in worker_token reader_token shim_token broker_token s3_secret; do [ -f "$S/$t" ] || rnd > "$S/$t"; done
printf '"%s": system:serviceaccount:replicant-workers:replicant-worker\n' "$(cat "$S/worker_token")" > "$S/runner_worker_tokens"
printf '"%s": repo-mirror\n' "$(cat "$S/reader_token")" > "$S/runner_revocations_readers"
printf '"%s": svc_replicant-services\n' "$(cat "$S/shim_token")" > "$S/gateway_service_tokens"
printf '[default]\naws_access_key_id = admin\naws_secret_access_key = %s\n' "$(cat "$S/s3_secret")" > "$S/s3_credentials"
cat > "$S/s3.json" <<EOF
{"identities":[{"name":"admin","credentials":[{"accessKey":"admin","secretKey":"$(cat "$S/s3_secret")"}],"actions":["Admin","Read","List","Tagging","Write"]}]}
EOF
chmod 0644 "$S"/*
# script root for the shim: GATEWAY's walkthrough set plus runner-contract scenarios
rm -rf "$SMOKE_HOME/scripts"; mkdir -p "$SMOKE_HOME/scripts"
cp -r "$ROOT/tests/fixtures/gateway-scripts/walkthrough-ledger-api" "$SMOKE_HOME/scripts/"
cp -r "$ROOT/tests/fixtures/gateway-scripts/runner-contract" "$SMOKE_HOME/scripts/"

podman network create "$NET" >/dev/null
podman volume create run-runner-state >/dev/null
podman volume create run-mirror-cache >/dev/null

podman run -d --name run-s3 --network "$NET" -v "$S/s3.json:/etc/s3.json:ro" docker.io/chrislusf/seaweedfs:4.48 \
  server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/s3.json -master.volumeSizeLimitMB=64 >/dev/null
podman run -d --name run-temporal --network "$NET" --user 0:0 docker.io/temporalio/admin-tools:1.32.1 \
  temporal server start-dev --ip 0.0.0.0 --port 7233 --headless --namespace replicant-local --log-level warn >/dev/null

podman run -d --name run-driver --network "$NET" -v "$ROOT:/src:ro" -v run-gomod:/go/pkg/mod -v run-gocache:/root/.cache \
  -v run-runner-state:/runner-state:ro -v "$S:/secrets:ro" \
  -e GOWORK=off -e GOFLAGS=-mod=mod -e S3_ENDPOINT=http://run-s3:8333 -e S3_ACCESS_KEY=admin -e "S3_SECRET_KEY=$(cat "$S/s3_secret")" \
  -e RUNNER_URL=http://run-runner:8480 -e RUNNER_SANDBOX_URL=http://run-runner:8490 -e SHIM_URL=http://run-shim:8420 \
  -e WORKER_TOKEN_FILE=/secrets/worker_token -e READER_TOKEN_FILE=/secrets/reader_token -e SHIM_TOKEN_FILE=/secrets/shim_token \
  -e "BROKER_CALLER_TOKEN=$(cat "$S/broker_token")" -e TEMPORAL_ADDRESS=run-temporal:7233 -e RUNNER_STATE_DIR=/runner-state \
  -e SAMPLE_REPO=/src/dev/sample-repos/ledger-api -e RESULTS_FILE=/tmp/smoke-results.json \
  -w /src/go/cmd/runner-local docker.io/library/golang:1.26 \
  sh -c "go build -o /tmp/smoke ./smoke && exec /tmp/smoke $*" >/dev/null

podman run -d --name run-shim --network "$NET" -v "$ROOT/dev/compose/config/gateway-shim:/etc/gateway-shim:ro" \
  -v "$SMOKE_HOME/scripts:/cassettes/scripts:ro" -v "$S:/run/secrets:ro" \
  -e REPLICANT_ENV=local -e REPLICANT_RUN_TOKEN_ISSUERS=http://run-runner:8480 -e REPLICANT_RUN_TOKEN_JWKS_URLS=http://run-runner:8490/.well-known/jwks.json \
  -e REPLICANT_RUN_GRANT_ISSUER=replicant-local-core -e REPLICANT_RUN_GRANT_JWKS_URL=http://run-driver:8403/.well-known/jwks.json \
  -e REPLICANT_RUN_GRANT_REVOCATION_URL=http://run-driver:8403/grants/revoked -e REPLICANT_EXEC_REVOCATIONS_URLS=http://run-runner:8480/v1/revocations \
  -e REPLICANT_EXEC_REVOCATIONS_TOKEN_FILE=/run/secrets/reader_token -e REPLICANT_EXEC_REVOCATIONS_POLL_SECONDS=10 \
  -e REPLICANT_GATEWAY_SERVICE_TOKENS_FILE=/run/secrets/gateway_service_tokens -e REPLICANT_GATEWAY_CASSETTE_ROOTS=scripts=/cassettes/scripts \
  -e REPLICANT_GATEWAY_LOCAL_QUOTA=monthly=100,daily=10,platform_daily=50,platform_monthly=500 \
  -e REPLICANT_GATEWAY_USAGE_S3_URI=file:///tmp/gateway-usage \
  localhost/replicant/replicant-gateway-shim:dev replicant-gateway-shim --config=/etc/gateway-shim/gateway-shim.yaml --upstream-backend=script >/dev/null

podman run -d --name run-mirror --network "$NET" -v run-mirror-cache:/var/cache/repo-mirror -v "$S:/run/secrets:ro" \
  -e MIRROR_RUN_TOKEN_ISSUERS=http://run-runner:8480 -e MIRROR_RUN_TOKEN_JWKS_URLS=http://run-runner:8490/.well-known/jwks.json \
  -e MIRROR_RUN_GRANT_ISSUER=replicant-local-core -e MIRROR_RUN_GRANT_JWKS_URL=http://run-driver:8403/.well-known/jwks.json \
  -e MIRROR_RUN_GRANT_REVOCATION_URL=http://run-driver:8403/grants/revoked -e MIRROR_EXEC_REVOCATIONS_URLS=http://run-runner:8480/v1/revocations \
  -e MIRROR_EXEC_REVOCATIONS_TOKEN_FILE=/run/secrets/reader_token -e MIRROR_UPSTREAMS=gitea=http://run-driver:3300 \
  -e MIRROR_BROKER_URL=http://run-driver:8081 -e MIRROR_BROKER_TOKEN_FILE=/run/secrets/broker_token \
  localhost/replicant/repo-mirror:dev >/dev/null

podman run -d --name run-runner --network "$NET" --security-opt label=disable -v "$SOCK:/var/run/docker.sock" \
  -v run-runner-state:/var/lib/runner-local -v "$S:/run/secrets:ro" \
  -v "$ROOT/dev/compose/config/runner-local/runner-local.yaml:/etc/runner-local/runner-local.yaml:ro" \
  -e RUNNER_CONFIG=/etc/runner-local/runner-local.yaml -e RUNNER_ISSUER=http://run-runner:8480 \
  -e RUNNER_SIGNING_KEY_FILE=/run/secrets/run_token_signing_key -e RUNNER_WORKER_TOKENS_FILE=/run/secrets/runner_worker_tokens \
  -e RUNNER_REVOCATIONS_READERS_FILE=/run/secrets/runner_revocations_readers -e RUNNER_CONTAINER_HOST=unix:///var/run/docker.sock \
  -e RUNNER_STATE_DIR=/var/lib/runner-local -e RUNNER_STATE_VOLUME=run-runner-state -e RUNNER_SANDBOX_NETWORK="$NET" \
  -e RUNNER_SANDBOX_DIRECT_EGRESS=true -e RUNNER_SANDBOX_GATEWAY_URL=http://run-shim:8420 -e RUNNER_SANDBOX_RUNNER_URL=http://run-runner:8490 \
  -e RUNNER_SANDBOX_REPO_MIRROR_URL=http://run-mirror:8080 -e RUNNER_SANDBOX_S3_ENDPOINT=http://run-s3:8333 \
  -e RUNNER_S3_ENDPOINT=http://run-s3:8333 -e RUNNER_S3_CREDENTIALS_FILE=/run/secrets/s3_credentials \
  -e RUNNER_GRANTS_ISSUER=replicant-local-core -e RUNNER_GRANTS_JWKS_URL=http://run-driver:8403/.well-known/jwks.json \
  -e RUNNER_GRANTS_REVOCATION_URL=http://run-driver:8403/grants/revoked -e RUNNER_GRANTS_REVOCATION_POLL_SECONDS=5 \
  -e RUNNER_REVOCATIONS_BUCKET=local-replicant-local-ops -e RUNNER_REVOCATIONS_PREFIX=exec-revocations/local/ \
  -e RUNNER_TEMPORAL_ADDRESS=run-temporal:7233 -e RUNNER_TEMPORAL_NAMESPACE=replicant-local -e RUNNER_MAX_CONCURRENT_RUNS=3 \
  -e RUNNER_CONTAINER_TTL=10m -e REPLICANT_KILL_SWITCH_MIRROR=s3://local-replicant-local-ops/kill-switches/v1/state.json \
  -e REPLICANT_ENV=local -e REPLICANT_ORG=local \
  localhost/replicant/runner-local:dev >/dev/null

echo "stack up; following the smoke driver (podman logs -f run-driver)"
podman logs -f run-driver &
LOGPID=$!
RC="$(podman wait run-driver)"
sleep 1; kill $LOGPID 2>/dev/null || true
podman cp run-driver:/tmp/smoke-results.json "$SMOKE_HOME/results.json" 2>/dev/null || true
echo "driver exit $RC (results: $SMOKE_HOME/results.json)"
exit "$RC"
