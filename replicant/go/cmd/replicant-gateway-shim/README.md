# replicant-gateway-shim

WS-03 AI gateway shim (spec `03-agents-and-tools/gateway.md`, `gateway-interface.openapi.yaml`,
`11-local-dev/gateway-stub.md`). Go 1.26, own module (`replicant.example.com/go/cmd/replicant-gateway-shim`), deps:
`gopkg.in/yaml.v3`, `github.com/klauspost/compress` (zstd).

```
replicant-gateway-shim --config=/etc/gateway-shim/gateway-shim.yaml --upstream-backend=script|passthrough|enterprise [--state=local]
replicant-gateway-shim healthcheck --url http://127.0.0.1:8420/readyz
replicant-gateway-shim devtool mint|register ...      # local tests only (dev keys, file JWKS, run token, policy PUT)
```

## What v0 implements

| Area | Status |
|---|---|
| `POST /anthropic/v1/messages` (stream + JSON), `/anthropic/v1/messages/count_tokens`, `/openai/v1/responses`, `/embeddings/v1/embeddings` | yes (script backend: Anthropic only) |
| Run token (`aud == ai-gateway`, ES256 vs `REPLICANT_RUN_TOKEN_JWKS_URLS` paired with issuers) + embedded run grant (core JWKS, subset checks), header/claim match | yes |
| Revocation: `self:close`, `POST /internal/v1/revocations`, `/grants/revoked` poll (60 s, ETag), exec feed `GET /v1/revocations` (10 s, cursor) | yes |
| `PUT/GET /internal/v1/runs/{run_id}/policy` (idempotent, 409 on change, `stub` only on local backends), `PUT /internal/v1/jobs/{job_id}/budget`, `PUT /internal/v1/budgets/jobs/{job_id}`, `GET /internal/v1/models`, `GET /v1/budget` | yes |
| Budgets: reservation + settlement at run / job / tenant month+day (or account) / platform day+month; 402 + `X-Replicant-Budget-Exceeded`; budget headers; run concurrency 429 | yes |
| Tenant quota from control-plane-api `GET /internal/v1/tenants/{t}/gateway-quota` (60 s cache, 404 ⇒ `tenant_quota_unset`) | yes; local fallback `REPLICANT_GATEWAY_LOCAL_QUOTA` |
| Usage events `replicant.gateway-usage/v1` → durable log (`platform/gateway-usage/<yyyy>/<mm>/<dd>/<hh>/<pod>-<seq>.ndjson.zst`, S3 SigV4 or a local dir) + usage-ingestor fast path | yes |
| Backends `script` (`replicant.gateway-script/v1`), `passthrough` (dev key), `enterprise` (= passthrough adapter) | yes; `record`/`replay` refuse to start |
| State | in-memory (`--state=local`); Valkey not in v0 (DEVIATIONS GATEWAY-D1) |

Not in v0: Prometheus metrics / OTel spans (structured logs only), tenant RPM/TPM limits, unparsed-usage per-minute
admission cap, Bedrock/Vertex routes. See `docs/v0/DEVIATIONS.md` (GATEWAY-D*).

## Script backend

Selection: the run policy `stub` (`{root, set, key}`; root `tests` is also looked up under the `scripts` root), tried as
`<root>/<set>/<key>.yaml`, then with trailing `.segments` removed (`implementer.c0.n1` → `implementer.c0` →
`implementer`), then `<set>/<role>.yaml`. Without a stub: `REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET` + the run role.

Turn semantics (Claude Code 2.1.310, verified with the real CLI): a per-run cursor advances over **main-loop** requests
(those carrying `tools`); a retried request (same `messages` length) gets the same turn; a `fault` turn is consumed
once. Tool-less requests (titles, classifiers) get `"OK."` without consuming a turn. `structured_output_from` /
`structured_output` becomes a call of the CLI's `StructuredOutput` tool (`--json-schema`); if the CLI answers it, the
shim replies with `finish` text. Strings may use `${cwd}` (the CLI's "Primary working directory", which Claude Code
2.1.3xx sends as a `role: system` message). `expect: {model, tools}` mismatches and an exhausted script are
`599 cassette_miss`. `X-Replicant-Stub-Fault: <status>[;retry_after=N][;budget_level=L][;code=C]` injects faults
(ignored by the enterprise backend).

Fixtures: `tests/fixtures/gateway-scripts/walkthrough-ledger-api/{implementer,reviewer}.yaml`.

## Build, test, smoke

```sh
# from the monorepo root
podman build -f go/cmd/replicant-gateway-shim/Dockerfile -t localhost/replicant/replicant-gateway-shim:dev .
podman run --rm -v "$PWD":/replicant:ro -w /replicant/go/cmd/replicant-gateway-shim -e GOWORK=off -e GOFLAGS=-buildvcs=false \
  docker.io/library/golang:1.26 sh -c 'go vet ./... && go test -race ./...'
# real `claude -p` (host install) fixes ledger-api through the shim (script mode, contract fakes for runner/grants):
go/cmd/replicant-gateway-shim/smoke/claude-smoke.sh [workdir]
```

## Environment (beyond the spec list in gateway.md §4.1)

| Variable | Meaning |
|---|---|
| `REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET` | script set for runs whose policy has no `stub` (local) |
| `REPLICANT_GATEWAY_LOCAL_QUOTA` | `monthly=…,daily=…,platform_daily=…,platform_monthly=…` used when control-plane-api is unset/unreachable (local only; a 404 still denies) |
| `REPLICANT_GATEWAY_USAGE_LOG_DIR` | local usage-log directory when `REPLICANT_GATEWAY_USAGE_S3_URI` is empty (`file://` URIs also work) |
| `REPLICANT_GATEWAY_USAGE_SSE_KMS_KEY_ID` | optional SSE-KMS key for usage-log PUTs |
| `ANTHROPIC_API_KEY_FILE` / `OPENAI_API_KEY_FILE` / `VOYAGE_API_KEY_FILE` | passthrough keys from a secret file (the plain variables also work); never logged |
| `REPLICANT_LOG_LEVEL` | `debug`/`info`/`warn` |

Static service tokens (`REPLICANT_GATEWAY_SERVICE_TOKENS_FILE`): YAML `token: subject`; subjects map to principals
(`system:serviceaccount:<ns>:replicant-worker` → `svc_replicant-worker`); `svc_replicant-services` / `svc_local-dev` hold
every local service right (PLATFORM's shared `gateway_service_token`).
