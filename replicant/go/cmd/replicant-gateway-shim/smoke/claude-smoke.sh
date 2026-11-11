#!/usr/bin/env bash
# Component smoke test (contract fakes): the REAL Claude Code CLI (`claude -p`, host install) drives its tool-use
# loop against replicant-gateway-shim in script mode and fixes the ledger-api sample bug (INTERFACES-LOG PLATFORM-01).
#
#   go/cmd/replicant-gateway-shim/smoke/claude-smoke.sh [workdir]
#
# Needs: podman, the image localhost/replicant/replicant-gateway-shim:dev, `claude` (2.1.310) and git on the host.
# Contract fakes instead of neighbours: dev ES256 keys + file:// JWKS (devtool mint) stand in for runner-local and
# run-grants; the run token goes straight into ANTHROPIC_AUTH_TOKEN with the X-Replicant-* headers that agentd would
# add (ANTHROPIC_CUSTOM_HEADERS). Tokens are written to files and never printed. Containers are named gw-*.
set -euo pipefail
ENGINE="${REPLICANT_CONTAINER_CLI:-podman}"
Z=""; if [ "$ENGINE" = podman ]; then Z=":Z"; fi   # SELinux relabel: a Podman-only volume option
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../../.." && pwd)            # monorepo root (replicant/)
spec_cfg=${GATEWAY_CONFIG_DIR:-$root/dev/compose/config/gateway-shim}
W=${1:-$(mktemp -d)}
PORT=${GW_PORT:-18420}
IMG=localhost/replicant/replicant-gateway-shim:dev
mkdir -p "$W/keys" "$W/secrets" "$W/usage" "$W/evidence"; chmod 777 "$W/usage"
cleanup() { "$ENGINE" rm -f -v gw-smoke-shim >/dev/null 2>&1 || true; }
trap cleanup EXIT

"$ENGINE" run --rm --name gw-smoke-mint -v "$W/keys:/keys$Z" --user 0 --entrypoint /usr/local/bin/replicant-gateway-shim "$IMG" \
  devtool mint --dir /keys --role implementer --budget 15
chmod 644 "$W/keys/"*.json "$W/keys/run-token"
python3 -c "import secrets;print(secrets.token_urlsafe(32),end='')" > "$W/secrets/svc_token"
printf '"%s": svc_replicant-services\n' "$(cat "$W/secrets/svc_token")" > "$W/secrets/gateway_service_tokens"
chmod 644 "$W/secrets/"*

"$ENGINE" rm -f -v gw-smoke-shim >/dev/null 2>&1 || true
"$ENGINE" run -d --name gw-smoke-shim -p "127.0.0.1:$PORT:8420" \
  -v "$spec_cfg:/etc/gateway-shim:ro" -v "$root/tests/fixtures/gateway-scripts:/cassettes/scripts:ro" \
  -v "$W/keys:/keys:ro" -v "$W/secrets:/run/secrets:ro" -v "$W/usage:/usage" \
  -e REPLICANT_ENV=local \
  -e REPLICANT_RUN_TOKEN_ISSUERS=http://runner-local:8480 -e REPLICANT_RUN_TOKEN_JWKS_URLS=file:///keys/runner-jwks.json \
  -e REPLICANT_RUN_GRANT_ISSUER=replicant-local-core -e REPLICANT_RUN_GRANT_JWKS_URL=file:///keys/grant-jwks.json \
  -e REPLICANT_GATEWAY_SERVICE_TOKENS_FILE=/run/secrets/gateway_service_tokens \
  -e REPLICANT_GATEWAY_CASSETTE_ROOTS=scripts=/cassettes/scripts \
  -e REPLICANT_GATEWAY_LOCAL_QUOTA=monthly=100,daily=50,platform_daily=500,platform_monthly=2000 \
  -e REPLICANT_GATEWAY_USAGE_S3_URI=file:///usage \
  "$IMG" replicant-gateway-shim --config=/etc/gateway-shim/gateway-shim.yaml --upstream-backend=script >/dev/null
for _ in $(seq 1 20); do curl -sf "http://127.0.0.1:$PORT/readyz" >/dev/null && break; sleep 0.5; done
"$ENGINE" exec gw-smoke-shim replicant-gateway-shim devtool register --dir /keys --url http://127.0.0.1:8420 \
  --service-token-file /run/secrets/svc_token --stub tests/walkthrough-ledger-api/implementer.c0.n1 >/dev/null

rm -rf "$W/repo"; cp -R "$root/dev/sample-repos/ledger-api" "$W/repo"
(cd "$W/repo" && git init -q && git add -A && git -c user.email=smoke@replicant -c user.name=smoke commit -qm init)
python3 - "$root/../spec/03-agents-and-tools/schemas" "$W/implementer.schema.json" <<'PY'
import json, sys
d, out = sys.argv[1], sys.argv[2]
common = json.load(open(d + "/common.schema.json"))["$defs"]
def inline(x):
    if isinstance(x, dict):
        if "$ref" in x and "common" in x["$ref"]:
            return inline(common[x["$ref"].split("/")[-1]])
        return {k: inline(v) for k, v in x.items()}
    return [inline(v) for v in x] if isinstance(x, list) else x
json.dump(inline(json.load(open(d + "/role-result-implementer.schema.json"))["$defs"]["agent_report"]), open(out, "w"))
PY
sed '1,/^---$/d' "$W/repo/ISSUES/001.md" > "$W/prompt.md"
RUN=$(python3 -c "import json;print(json.load(open('$W/keys/run.json'))['run_id'])")
JOB=$(python3 -c "import json;print(json.load(open('$W/keys/run.json'))['job_id'])")
mkdir -p "$W/cfg"
(cd "$W/repo" && env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$W/cfg" \
  ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT/anthropic" ANTHROPIC_AUTH_TOKEN="$(cat "$W/keys/run-token")" \
  ANTHROPIC_CUSTOM_HEADERS="$(printf 'X-Replicant-Tenant-Id: acme-payments\nX-Replicant-Job-Id: %s\nX-Replicant-Run-Id: %s\nX-Replicant-Role: implementer' "$JOB" "$RUN")" \
  ANTHROPIC_DEFAULT_HAIKU_MODEL=anthropic/claude-haiku-5-5 CLAUDE_CODE_SUBAGENT_MODEL=anthropic/claude-sonnet-5-5 \
  CLAUDE_CODE_MAX_RETRIES=4 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 DISABLE_UPDATES=1 \
  DISABLE_ERROR_REPORTING=1 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 \
  perl -e 'alarm 300; exec @ARGV' claude -p --output-format stream-json --verbose \
    --model anthropic/claude-sonnet-5-5 --fallback-model anthropic/claude-opus-5-5 --max-turns 50 --max-budget-usd 15.00 \
    --permission-mode dontAsk --setting-sources user --tools Bash,Read,Edit,Write --allowedTools Bash,Read,Edit,Write \
    --json-schema "$(cat "$W/implementer.schema.json")" "$(cat "$W/prompt.md")" < /dev/null > "$W/evidence/claude-stream.jsonl")
git -C "$W/repo" diff > "$W/evidence/implementer.diff"
python3 - "$W/evidence/claude-stream.jsonl" <<'PY'
import json, sys
res = [json.loads(l) for l in open(sys.argv[1]) if '"type":"result"' in l][-1]
assert res["subtype"] == "success" and not res["is_error"], res.get("subtype")
assert res["structured_output"]["outcome"] == "completed"
print("claude -p result:", res["subtype"], "turns", res["num_turns"], "outcome", res["structured_output"]["outcome"])
PY
grep -q '^+		fxReversal := Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)$' "$W/evidence/implementer.diff"
"$ENGINE" run --rm --name gw-smoke-gotest -v "$W/repo:/r:ro" -w /r -e GOFLAGS=-buildvcs=false docker.io/library/golang:1.26 \
  sh -c 'go vet ./... && go build ./... && go test -count=1 ./...' | tee "$W/evidence/go-test.txt"
echo "SMOKE OK (evidence in $W/evidence)"
