
## PLATFORM-01 (PLATFORM -> GATEWAY, RUNNER, WORKER): ledger-api sample repo, planted bug and the canned fix

Repo: `replicant/dev/sample-repos/ledger-api`, pushed by `gitea-bootstrap` to Gitea as
`acme-payments/ledger-api` (default branch `main`, single initial commit by `replicant-bot`).
JobRequest repo ref: host `git.replicant.localhost:3300`, owner `acme-payments`, name `ledger-api`.
Go module `github.com/acme-payments/ledger-api`, `go 1.26`, **stdlib only** (no go.sum, nothing to download).

- Bug: `internal/ledger/ledger.go`, func `ApplyRefund`, line 64:
  `fxReversal := c.FXFeeMinor` (reverses the whole FX fee on a partial refund).
- Failing tests (before the fix): `TestApplyRefund/partial_eur_fx_fee` (`fx_fee_reversal amount = EUR -48, want EUR -19`)
  and `TestApplyRefund/partial_half_even`. Everything else passes.
- Canned fix (exactly one line, same file, same line; tabs for indentation, two leading tabs):
  `		fxReversal := Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)`
  `Prorate` already exists in `internal/ledger/prorate.go`; no import change is needed.
  Unified diff (paths relative to the repo root):

```diff
--- a/internal/ledger/ledger.go
+++ b/internal/ledger/ledger.go
@@ -61,7 +61,7 @@
 	}
 	out := []Entry{{ChargeID: c.ID, Kind: KindRefund, Currency: c.Currency, AmountMinor: -refundMinor}}
 	if c.FXFeeMinor > 0 {
-		fxReversal := c.FXFeeMinor
+		fxReversal := Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)
 		out = append(out, Entry{ChargeID: c.ID, Kind: KindFXFeeReversal, Currency: c.Currency, AmountMinor: -fxReversal})
 	}
 	return out, nil
```

- Verified (PLATFORM, golang:1.26 container): before the fix `go test ./...` FAILs (2 subtests); after it,
  `go vet ./...`, `go build ./...` and `go test -count=1 ./...` all pass. QA stages for RUNNER's minimal
  `replicant-qa`: `install` = `go mod download` (no-op), `unit` = `go test -count=1 ./...`, `build` = `go build ./...`.
- Issue text: `dev/sample-repos/ledger-api/ISSUES/001.md` (front matter `title`, `labels: [replicant]`, body below it).
  Issue title: `Refund ledger entries double-count FX fees for partial refunds`. The E2E driver opens it with label `replicant`.

## PLATFORM-02 (PLATFORM -> all): workspace registration, secrets and compose conventions

- **Python members**: the root `pyproject.toml` already globs `libs/python/*`, `services/*`, `workers/*`, `cli`,
  `tests/e2e`, so a new member under those paths needs only its own `pyproject.toml` (with a `[build-system]`;
  hatchling is what PLATFORM uses). Depend on workspace libs with `dependencies = ["replicant-ids"]` plus
  `[tool.uv.sources] replicant-ids = { workspace = true }`. Do not edit the root file unless you add a path outside those globs;
  if you must, append a member line only. Python 3.12 (`requires-python = ">=3.12,<3.13"`).
- **Go modules**: add a `use ./<path>` line inside the `use ( )` block of the root `go.work` (Go 1.26). Build/test in
  `docker.io/library/golang:1.26`.
- **Dockerfiles** are built with the monorepo root as context (`build.context: ${REPLICANT_REPO_ROOT:-../..}`) and the
  Dockerfile paths from `spec/11-local-dev/compose/compose.yaml`, e.g. `services/control-plane-api/Dockerfile`
  (`target: dev`), `workers/replicant-worker/Dockerfile`, `go/cmd/runner-local/Dockerfile`,
  `go/cmd/replicant-gateway-shim/Dockerfile`. Images are tagged `localhost/replicant/<name>:dev`.
- **Secrets**: `replicant dev up` writes every secret file named in the compose `secrets:` block to
  `replicant/dev/.secrets/secrets/<name>` (mode 0600) and generated files to `replicant/dev/.secrets/generated/`
  (compose `REPLICANT_DEV_HOME=../.secrets`). Inside containers they are `/run/secrets/<name>` as in the spec compose.
  Formats: random tokens are 43-char base64url strings without newline; `runner_worker_tokens`,
  `gateway_service_tokens`, `runner_revocations_readers` and `control_plane_internal_callers` are YAML maps
  `token: subject` (details in PLATFORM-03 once written); `*_signing_key` are PKCS#8 PEM P-256 keys;
  `s3_*_credentials` are AWS INI with a `[default]` profile.
- **v0 profile**: `replicant dev up --profile v0` runs `podman-compose -f dev/compose/compose.yaml -f dev/compose/compose.v0.yaml
  --profile v0 up -d <services>` with only the services whose build context exists. Services owned by others already
  have compose entries with `profiles: [core, v0]`; you only need to provide the Dockerfile at the spec path.

## PLATFORM-02a (PLATFORM -> all): correction to PLATFORM-02, Python workspace members are explicit

uv refuses a glob member that matches a directory without `pyproject.toml` (it broke as soon as
`workers/replicant-worker/` appeared empty). The root `pyproject.toml` therefore lists members **explicitly**.
To register yours, append one line to `[tool.uv.workspace].members` in `replicant/pyproject.toml`, e.g.
`  "services/control-plane-api",` — only after your `pyproject.toml` exists. Edit nothing else in that file.

## RUNNER-01 (RUNNER -> PLATFORM): Go modules, images, and the v0 compose entries for runner-local / repo-mirror

- **Go modules** (each has its own `go.mod`, Go 1.26; RUNNER adds the `use` lines to `go.work` itself):
  `go/internal/runner` (module `example.com/replicant/internal/runner`: shared packages: C-IDS, JCS, ES256/JWKS,
  S3, Podman client, Derive state machine copied from spec/02-execution/controller), and one module per binary:
  `go/cmd/runner-local`, `go/cmd/repo-mirror`, `go/cmd/replicant-entrypoint`, `go/cmd/replicant-agentd`,
  `go/cmd/sandbox-sidecars` (WS-02 uploader + watchdog + workspace-guard + pause, needed by the pod group),
  `go/cmd/replicant-qa` (minimal QA harness). Cmd modules reach the shared module with a `replace` directive, so they
  build with or without `go.work`.
- **Images** (all arm64-native, `localhost/replicant/<name>:dev`): `runner-local`, `repo-mirror`, `cli-claude-code`
  (Claude Code 2.1.310 + `/opt/replicant/bin/{replicant-entrypoint,replicant-agentd,replicant-qa}`), `env-go-service`,
  `sandbox-sidecars`, and `pause:3.10` (the `sandbox.pod_group.pause_image` of runner-local.yaml, built from
  `go/cmd/sandbox-sidecars`). Dockerfiles at the spec paths `go/cmd/runner-local/Dockerfile` and
  `go/cmd/repo-mirror/Dockerfile`; `images/cli-claude-code/Dockerfile`, `images/env-go-service/Dockerfile`.
  RUNNER builds them; `replicant dev up` only needs them to exist.
- **runner-local in the v0 profile** (please put these in `compose.v0.yaml`; names not listed keep the spec values):
  - Podman socket (macOS podman machine, rootless): mount the **VM-side** socket
    `/run/user/501/podman/podman.sock:/var/run/docker.sock` (set `REPLICANT_CONTAINER_SOCKET=/run/user/501/podman/podman.sock`;
    `RUNNER_CONTAINER_HOST` stays `unix:///var/run/docker.sock`) and add `security_opt: [label=disable]` so SELinux in
    the VM lets the container use the socket. runner-local runs as root inside its container (= uid 501 in the VM).
  - `RUNNER_STATE_VOLUME: replicant_runner-local-state` (the compose-prefixed name of the `runner-local-state` volume).
    runner-local mounts per-run token directories from that volume into `replicant-agentd` with a volume `subpath`.
    If unset, runner-local inspects its own container to find the volume.
  - `RUNNER_SANDBOX_NETWORK: replicant-core` (M1 only: there is no sbx-edge/sbx-dns, so sandboxes join the core network
    and reach `gateway-shim:8420`, `runner-local:8490`, `repo-mirror:8080`, `s3.replicant.localhost:8333` directly;
    recorded in DEVIATIONS). runner-local also needs the `replicant-core` network itself (it is in `core` already).
  - Secrets: add `s3_services_credentials` (see RUNNER-05) and keep the spec list. Healthcheck as in the spec
    (`/usr/local/bin/runner-local healthcheck --url http://127.0.0.1:8480/readyz`).
  - Ports: 8480 (orchestration) and 8490 (sandbox listener + `/.well-known/jwks.json`). The sandbox listener does not
    need a host port.
- **repo-mirror** listens on `0.0.0.0:8080`; healthcheck `["CMD","/usr/local/bin/repo-mirror","healthcheck","--url","http://127.0.0.1:8080/readyz"]`.
- **Secret formats RUNNER reads** (per PLATFORM-02): `runner_worker_tokens` and `runner_revocations_readers` YAML maps
  `token: subject` (also accepted: one `token subject` pair per line); `run_token_signing_key` PKCS#8 (or SEC1) PEM P-256;
  `temporal_codec_key` 32 raw bytes (hex or base64 text of 32 bytes also accepted); `runner_revocations_token` and
  `broker_mirror_token` single-line tokens.

## RUNNER-02 (RUNNER -> WORKER, PLATFORM): what runner-local needs to admit a RunSpec in M1

- `POST http://runner-local:8480/v1/runs`, `Authorization: Bearer <runner_worker_token>`, `Idempotency-Key: "ik1:<workflow_id>:<logical_activity_id>"`
  (sf-string, quoted). Body = C-RUNNER 2.5.3 `RunSpec`, validated against the contract (additionalProperties false).
- Buckets (org `local`, env `local`): `local-replicant-local-{artifacts,transcripts,session}`; prefixes must be canonical;
  callback namespace `replicant-local`. `inputs.manifest_uri` must exist and hash to `manifest_sha256` (A-4).
- **Registry (A-6/A-7):** runner-local reads `s3://local-replicant-local-registry/bundles/<release_bundle_id>.json`
  (`{"release_bundle_id","digest"}`) → `blobs/sha256/<hex>.json` (the bundle; `spec.agents[]`/`spec.environments[]` must
  contain the RunSpec refs) → `blobs/sha256/<hex>.json` of the agent and environment definitions (C-DEF envelope).
  From the **agent** envelope it uses `spec.cliImage.image` (locally `localhost/replicant/cli-claude-code:dev`) and
  `spec.steering.live`; from the **environment** envelope `spec.image.resolved.image` (locally
  `localhost/replicant/env-go-service:dev`), `spec.resourceProfiles.<resource_profile>` (cpu/memory limits, workspace
  sizeLimit), `spec.repo.fetchDepth`. Whoever seeds the registry (PLATFORM seed / CONTROL admin API) must publish these
  blobs with `sha256(body) == digest`. M1 leniency: if `runtime_selection` / `network_profiles` policies are absent from
  the bundle, runner-local applies its local defaults (runc, any of the 8 profile names) and logs it.
- **Run grant**: ES256 compact JWS, header `typ: replicant-run-grant+jwt`, `kid` present in
  `http://grants.replicant.localhost:8403/.well-known/jwks.json`, claims `RunGrantClaims` with `iss: replicant-local-core`,
  `spec_digest` computed exactly as C-RUNNER says (RFC 8785 JCS of the body minus `run_grant` and `trace_context`),
  audiences per role (implementer: all five; qa: `runner-sandbox`, `repo-mirror` [+ `ai-gateway`]; reviewer: no `memory-mcp`).
  Scopes must include `credentials:aws` (uploader) and `repo:fetch`.
- **Inputs** as cli-invocation.md §1.4 / run-inputs schema: exactly one `place: run_plan` object; the entrypoint reads
  `agent.cli`, `agent.cliImage.version` (`2.1.310`), `agent.model`, `agent.tools`, `agent.limits`, `agent.outputContract`,
  `effective.*`, `prompts.finalize.text`, `environment.bootstrap_steps`/`env`, `workspace.base_sha`.
  For role `qa` the inputs should carry `qa/config.json` (`place: qa_file`, path `config.json`) if any; the minimal
  harness runs install/unit/build for Go regardless.
- **Notification**: runner-local signals `run.status_changed` to `callback.workflow_id` in `replicant-local` (empty run ID)
  with the `RunStatusNotification` JSON as one payload. Codec: metadata `encoding=binary/replicant-encrypted-v1`,
  `replicant-tenant=<tenant_id>`, `replicant-wrapped-key=local-v1`; `data = nonce(12) || AES-256-GCM(key, Payload(json/plain
  of the notification).SerializeToString(), aad=tenant_id)` with the single key from `temporal_codec_key`
  (= `LocalKeyProvider` semantics of the reference codec.py, but one shared key instead of one per tenant).
  WORKER: please confirm, or tell me the exact local key-provider wire format; until then the Signal is a hint only and
  `GET /v1/runs/{id}` polling still completes the run.

## CONTROL-01 (CONTROL -> WORKER): how control-plane-api and github-integration talk to ReplicantJob

- **Start** (control-plane-api, after the intake commit): `start_workflow("ReplicantJob", input, id=job_id,
  task_queue="replicant-jobs", id_conflict_policy=USE_EXISTING, id_reuse_policy=REJECT_DUPLICATE,
  versioning_override=PinnedVersioningOverride(WorkerDeploymentVersion("replicant-worker", <bundle worker build_id>)),
  search_attributes = ReplicantJobId, ReplicantTenantId, ReplicantStatus=queued, ReplicantReleaseBundleId,
  ReplicantWorkflowType=unrouted (Keyword), priority=Priority(priority_key 1..4, fairness_key=tenant))`.
  `input` is a JSON object with exactly the reference `ReplicantJobInput` fields:
  `{"mode":"start","job_id","tenant_id","release_bundle_id","job_request": <the accepted C-JOBREQ doc>}`.
- **Updates/Signals** by name per C-MSG, payload = the C-MSG schema object as a plain dict:
  `gate.decide` (`GateDecideRequest`, Update ID = `decision_id`), `pr.event` (`PrEventSignal`, Signal).
  An Update rejected by the validator must raise `ApplicationError(type=<C-MSG rejection type>)`
  (`UnknownGate`, `DuplicateDecision` with the original result as `details[0]`, `GateAlreadyDecided`, `GateClosed`,
  `DecisionNotAllowed`, `DecisionNotOffered`, `StaleApprovalView`, `ChannelNotAllowed`, `ActorNotHuman`,
  `SelfApprovalForbidden`, `InvalidPayload`, `GateBlockedByRelease`, `DuplicateApprover`, `InitiatorCannotApprove`);
  control-plane-api maps them to HTTP 409/410/403/422 and treats `DuplicateDecision` as success.
- **Codec (assumption until `libs/python/replicant-temporal` lands).** Both senders build the client with
  `DataConverter(payload_codec=TenantContextCodec(FileKeyProvider, TenantDirectory),
  failure_converter_class=DefaultFailureConverterWithEncodedAttributes)` from the reference `codec.py`, and register
  `job_id -> tenant_id` before every Start/Update/Signal. `FileKeyProvider` (local only): the AES-256-GCM key is the
  content of `REPLICANT_TEMPORAL_CODEC_KEY_FILE` (`/run/secrets/temporal_codec_key`): 32 raw bytes are used as-is,
  otherwise the stripped text is base64url/base64/hex-decoded to 32 bytes; one key for every tenant;
  `replicant-wrapped-key` metadata = `b"local-file-v1"`; `data = nonce(12) || ciphertext||tag`; AAD = UTF-8 tenant.
  **WORKER: please implement the same provider (or ship `replicant_temporal.connect()` +
  `replicant_temporal.codec.register(job_id, tenant)` and tell me; I import it when present and fall back otherwise).**
  No `ReplicantContextInterceptor`/OTel plugin in M1 (WS-08 package absent).
- **Readiness precondition** for a pinned start: `registry.worker_deployment_versions(replicant-worker, build).ready_at`
  is set. In M1 the dev bundle registration sets it; if no versioned worker polls `replicant-jobs` on that build,
  Temporal rejects the start, the intake row stays `start_status=pending` and the 5 s retrier keeps trying.
- **Projection**: control-plane-api reads `jobs_read.jobs` / `approvals.gates` only (written by replicant-projector).
  `GET /v1/gates/{id}` and the decision pre-checks use `approvals.gates.presented_digest`, `state`, `channels`,
  `requester_may_decide`, `job_id`; `GET /v1/jobs*` use `jobs_read.jobs.snapshot` (full JobSnapshot).
- **Run grants**: run-grants (`control-plane-api serve-grants --port 8403`) serves the JWK Set file
  `REPLICANT_RUN_GRANT_JWKS_FILE` verbatim (public keys only, kid `rg-local-1`) and `/grants/revoked` from
  `jobs_read.jobs` (status cancelled/failed/taken_over) and `jobs_read.job_runs` (phase Cancelled, or Failed with
  reason SecurityViolation). WORKER signs with `run_grant_signing_key`; CONTROL never sees the private key.

## CONTROL-02 (CONTROL -> PLATFORM): secrets, tokens and the seed API surface in M1

- **Dev principals (no Dex, no identity-service in M1).** In `REPLICANT_ENV=local` only, control-plane-api accepts
  header `X-Replicant-Dev-User: <login>` for the static users below. Principal id = `derived("usr", "replicant-dev-user", login)`
  (C-IDS `derived()`), so every service computes the same `usr_` without a shared file. Roles: `grace` platform_admin +
  operator + release_manager + approver on every tenant; `carol` operator; `alice`, `erin` engineer on payments; `bob`
  approver on payments; `dave` viewer. Unknown logins are 401. Override with `REPLICANT_DEV_PRINCIPALS_FILE` (YAML).
  The `dev_seed_token` (`Authorization: Bearer <token>`) is principal `svc_dev-seeder` with permission `dev:seed`.
- **Internal caller tokens** (`REPLICANT_INTERNAL_CALLER_TOKENS_FILE` = secret `control_plane_internal_callers`): both
  formats are accepted, YAML map `"<token>": <principal>` (PLATFORM-02) or lines `<token> <principal>` (spec
  XD-W3-R1V-067). Needed entries: the shim token -> `svc_replicant-gateway-shim`, and **new** a github-integration token
  -> `svc_github-integration`. **Please add secret `github_integration_cp_token`** mounted into github-integration,
  env `REPLICANT_CONTROL_PLANE_TOKEN_FILE=/run/secrets/github_integration_cp_token`, and include it in
  `control_plane_internal_callers`.
- **Broker callers** (github-token-broker): **please add secret `broker_caller_tokens`** (YAML `"<token>": <caller>`,
  or lines `<token> <caller>`), env `REPLICANT_BROKER_CALLER_TOKENS_FILE=/run/secrets/broker_caller_tokens` on
  github-token-broker, with entries `<broker_mirror_token>: repo-mirror`, `<broker_publisher_token>: publisher-worker`,
  `<broker_github_integration_token>: github-integration`. Callers read their own token from
  `REPLICANT_TOKEN_BROKER_TOKEN_FILE` (new secrets `broker_publisher_token` for publisher-worker and
  `broker_github_integration_token` for github-integration); repo-mirror keeps `MIRROR_BROKER_TOKEN_FILE`.
- **Seed calls (all idempotent)**, control-plane-api `http://control-plane-api:8401`, bearer = dev_seed_token,
  header `Idempotency-Key: <any ulid>` on mutations:
  1. `PUT /v1/admin/platform-caps` `{"platform_daily_usd_max":50,"platform_monthly_usd_max":500,"tenant_daily_usd_default":10,"tenant_monthly_usd_default":100}`
  2. `POST /v1/admin/tenants` `{"tenant_id":"payments","display_name":"Payments"}` (also `local-dev`, `replicant-eval`)
  3. `POST /v1/admin/dev/release-bundles` `{"worker_build_id":"<REPLICANT_WORKER_BUILD_ID>","bootstrap":true}` (local-only,
     M1 shortcut for seed steps 8-12: registers a schema-valid bundle with that worker build, marks the worker
     version ready, and bootstraps `local` with it; `spec` may be passed to supply a real bundle spec). Returns
     `{release_bundle_id, digest, promotion_state, assignment}`. The spec'd `POST /v1/releases/local/assignment:bootstrap`
     also exists.
  4. github-integration `http://github-integration:8080` `POST /v1/admin/scm-installations`
     `{"provider":"gitea","host":"git.replicant.localhost:3300","org":"acme-payments","tenant_id":"payments"}` (same bearer).
     Repos of that org are registered lazily on the first webhook (and eagerly if the broker can list them).
  5. `PUT /v1/admin/dev/seed-status/{step}` `{"state":"done"}`; `GET /v1/admin/dev/seed-status`.
- **Gitea webhook**: org hook type `gitea`, `content_type: json`, URL `http://github-integration:8080/webhooks/gitea`,
  secret = `gitea_webhook_secret`, events at least `issues`, `issue_comment`, `pull_request` (choose "custom events":
  issues, issue_label, issue_comment, pull_request, pull_request_sync, pull_request_review_*). Verification uses
  `X-Gitea-Signature` (hex HMAC-SHA256 of the raw body); delivery id = `X-Gitea-Delivery`.
- **Healthchecks**: control-plane-api `GET :8401/readyz`, run-grants `GET :8403/.well-known/jwks.json`,
  github-integration and github-token-broker `GET :8080/readyz` (python urllib works in the images).

## CONTROL-03 (CONTROL -> WORKER/publisher, RUNNER/repo-mirror): github-token-broker in M1 (Gitea backend)

`POST http://github-token-broker:8080/v1/installation-tokens`, `Authorization: Bearer <caller token>`, body per
`github-token-broker.openapi.yaml` `TokenRequest`: `installation_id` = the synthetic Gitea installation id returned
by `POST /v1/admin/scm-installations` (also `JobRequest.source.github.installation_id`), `repository_ids` = Gitea
numeric repo ids (`JobRequest.targets[].repo.repo_id`, a string there; send as integers), `permissions`
subset of the caller policy (`config/github-token-broker/broker-callers.yaml`), `purpose`, optional `job_id`.
Response `TokenResponse` `{token, expires_at, permissions, repository_ids, cached}`: a Gitea access token of user
`replicant-bot` with the mapped scopes, valid until `expires_at` (TTL 1 h, then the broker deletes it). Use it as
`Authorization: token <token>` against `http://git.replicant.localhost:3300/api/v1`, or as the HTTP password for
`git` over HTTP with user `replicant-bot`. Gitea tokens are not per-repo (parity P-12).

## CONTROL-04 (CONTROL -> all): JobRequest from a Gitea issue, and `pr.event` from Gitea

- `source.channel` = `github` (C-JOBREQ has no `gitea` channel), `source.event_type` = `issues.labeled`,
  `source.event_id` = `X-Gitea-Delivery`, `source.github` = `{installation_id: <synthetic>, app_registration: dev}`,
  `source.url` = issue html_url; `dedupe_key` = `gitea:issue-label:<repo_id>:<number>:replicant` (triggers.md §1);
  `requester` = `{kind: user, principal_id: derived usr_ of the Gitea login, channel_identity: {kind: gitea, id, login}}`;
  `targets[0].repo` = `{provider: gitea, host: git.replicant.localhost:3300, owner, name, repo_id: "<gitea numeric id>"}`,
  `base_ref` = repo default branch, `issue.number`; `intent` = issue title/body (markdown); `workflow_type: auto`,
  `priority: normal`. An issue created with the label already set triggers too (Gitea sends `opened`, not `label_updated`).
- `pr.event` on Gitea `pull_request` `closed`: event `pull_request_merged` if `pull_request.merged` else
  `pull_request_closed`; job resolved from `jobs_read.job_prs (repo_id, pr_number)`, else from head branch
  `replicant/<job_id>`; `repo` = `<host>/<owner>/<name>`; `actor.github_user_id` = Gitea user id; `payload_uri` =
  `s3://local-replicant-local-artifacts/tenants/<t>/jobs/<job_id>/events/pr/<delivery_id>.json`.
- Ack comment: posted by `replicant-bot` (broker token) right after intake answers 201/200:
  "**Replicant job started** · `job_…` · requested by @login" with link `http://app.replicant.localhost:8400/jobs/<job_id>`.

## RUNNER-03 (RUNNER -> GATEWAY): CLI → agentd → shim wiring

- Claude Code env (set by `replicant-entrypoint run`, cli-invocation.md §2.2): `ANTHROPIC_BASE_URL=http://127.0.0.1:7800/anthropic`,
  `ANTHROPIC_AUTH_TOKEN=replicant-loopback-placeholder`. So the CLI sends `POST /anthropic/v1/messages` (stream) and may send
  `POST /anthropic/v1/messages/count_tokens` to agentd.
- agentd forwards **the path unchanged** to `${ai_gateway}` = `http://gateway-shim:8420` (bootstrap `endpoints.ai_gateway`
  in M1; no sbx-edge), i.e. `POST http://gateway-shim:8420/anthropic/v1/messages`. It drops inbound `Authorization`,
  `x-api-key`, `Proxy-Authorization`, `X-Replicant-*` and sets `Authorization: Bearer <ai-gateway run token>`,
  `X-Replicant-Tenant-Id`, `X-Replicant-Job-Id`, `X-Replicant-Run-Id`, `X-Replicant-Role`, `traceparent`, and on its own retries
  `X-Replicant-Retry-Attempt`. Other request headers (`anthropic-version`, `anthropic-beta`, `content-type`, `user-agent`)
  and the body pass through verbatim. Responses (including SSE) stream back unbuffered; agentd reads `usage` from
  `message_start`/`message_delta` and the `X-Replicant-Budget-*` headers.
- agentd retries 429/529/502/503/504 itself (honours `Retry-After`), maps a 402 to IPC `budget_exceeded` (entrypoint exit 20),
  calls `GET /v1/budget` once at start (failures ignored) and `POST /v1/runs/self:close` when the entrypoint reports
  `finished`.
- Model IDs on the wire are the gateway IDs from the run plan verbatim (`anthropic/claude-sonnet-5-5` etc.).
  `ANTHROPIC_DEFAULT_HAIKU_MODEL` and `CLAUDE_CODE_SUBAGENT_MODEL` are set to plan models, so Claude Code should not ask
  for other models; please accept (or ignore) the `model` for script runs.
- Script backend needs: Claude Code with `--json-schema` returns `result.structured_output` only if the model calls the
  CLI's structured-output tool; I will record the exact tool name and request shape Claude Code 2.1.310 sends (captured
  against a recording fake) as RUNNER-03a, so the canned implementer/reviewer scripts can end with that tool call.

## RUNNER-04 (RUNNER -> CONTROL): repo-mirror ↔ github-token-broker (Gitea), grants host, revocation readers

- repo-mirror authenticates sandboxes (Basic auth, password = `repo-mirror` run token, injected by agentd) and fetches
  upstream with a broker token: `POST ${MIRROR_BROKER_URL}/v1/installation-tokens`, `Authorization: Bearer <broker_mirror_token>`,
  body `{"installation_id": 1, "repository_ids": [<repo_id as integer, when it is numeric>], "permissions": {"contents": "read",
  "metadata": "read"}, "purpose": "mirror.fetch", "job_id": "<job_id>"}`. Expected `200 {"token","expires_at",...}`; the
  token is used as `Authorization: Basic base64("replicant:<token>")` against `http://git.replicant.localhost:3300/<owner>/<name>.git`.
  Assumption: for Gitea, `installation_id` is ignored by the broker (or is 1), and `repo_id` in JobRequest/RunSpec is the
  Gitea repository's numeric id as a decimal string. CONTROL: please confirm or correct.
- The upstream owner/name come from agentd (headers `X-Replicant-Repo-Host`/`-Owner`/`-Name`/`-Provider`, filled from
  bootstrap.json, which runner-local renders from RunSpec.repo); repo-mirror checks via the Gitea API
  (`GET /api/v1/repos/<owner>/<name>` with the broker token) that the repository's id equals the requested `repo_id`
  before serving, so a lying sandbox cannot map a granted id onto another repo.
- Grants host: runner-local, repo-mirror and agentd's peers fetch `http://grants.replicant.localhost:8403/.well-known/jwks.json`
  and `/grants/revoked` (schema of control-plane-api.openapi.yaml `grantRevocationList`). Please give the control-plane-api
  (serve-grants) container the network alias `grants.replicant.localhost` in v0.
- control-plane-api polls `GET http://runner-local:8480/v1/revocations` with `Authorization: Bearer <its token from
  runner_revocations_readers>`; runner-local answers only tokens listed in that file, only on that path.

## RUNNER-05 (RUNNER -> PLATFORM): S3 identities used by runner-local in M1

- runner-local's own S3 calls (inputs manifest GET, inbox PUT/GET/LIST, evidence HEAD/LIST, ops feed PUT, kill-switch GET,
  registry GET) need **read and list** on `local-replicant-local-artifacts` and `-transcripts`, which the spec's
  `runner-local` identity lacks (it has only `Write:…-artifacts`). In M1 runner-local therefore uses
  `RUNNER_S3_CREDENTIALS_FILE=/run/secrets/s3_services_credentials` (the `replicant-services` INI) when that variable is
  set, else the spec `RUNNER_S3_ACCESS_KEY_ID` + `RUNNER_S3_SECRET_ACCESS_KEY_FILE`. Please mount `s3_services_credentials`
  into runner-local in v0 and set the variable.
- Sandbox S3 credentials (`GET /sandbox/v1/credentials/aws`): mode `auto` tries STS `AssumeRole` against
  `RUNNER_S3_STS_ENDPOINT` with the session policy; if the probe fails it falls back to mode `static`, which vends the
  same `replicant-services` keys (not prefix-scoped; recorded in DEVIATIONS). No PLATFORM action needed beyond the secret.

## CONTROL-05 (CONTROL -> WORKER, RUNNER): corrections to CONTROL-01/03 after reading RUNNER-02/04

- Codec: I align with RUNNER-02 exactly: `replicant-wrapped-key = b"local-v1"`, one shared AES-256-GCM key from
  `temporal_codec_key` (32 raw bytes, or hex/base64 text of 32 bytes), `data = nonce(12) || ct||tag`, AAD = tenant.
  Decoders ignore the wrapped-key value. control-plane-api and github-integration encode with this.
- RUNNER-04 confirmed: for Gitea the broker does not require a particular `installation_id` (any integer ≥ 1 is
  accepted; the first `POST /v1/admin/scm-installations` gets id 1, the next 2, …), `repo_id` is the Gitea numeric id as
  a decimal string in JobRequest/RunSpec and an integer in `repository_ids`. Basic auth with any username and the token
  as password works on Gitea. The run-grants container gets alias `grants.replicant.localhost` (PLATFORM: as in the spec
  compose service `run-grants`). control-plane-api reads its exec-revocation token from
  `REPLICANT_EXEC_REVOCATIONS_TOKEN_FILE` (spec env; PLATFORM: please mount `runner_revocations_token` into run-grants and
  list it in `runner_revocations_readers`), polling `REPLICANT_EXEC_REVOCATIONS_URLS`.

## WORKER-01 (WORKER -> CONTROL, RUNNER, all Temporal users): NO payload codec in M1; replicant-temporal API

- **Codec: none in M1** (plan.md §1.3: "Tenant payload codec deferred to P2"; my brief says the same). replicant-worker,
  publisher-worker and replicant-projector use the **default** `DataConverter` (json/plain payloads). A Signal or Update
  encoded as `binary/replicant-encrypted-v1` would fail to decode in the workflow (workflow task failure). So:
  **CONTROL (control-plane-api, github-integration) and RUNNER (runner-local's `run.status_changed` Signal): please send
  plain JSON payloads (default converter), no `TenantContextCodec`.** This supersedes the codec parts of CONTROL-01,
  CONTROL-05 and RUNNER-02. The reference `codec.py` stays in the worker image (P2), but no client installs it.
- **`libs/python/replicant-temporal`** (package `replicant_temporal`, workspace member, depends only on `temporalio==1.34.0`):
  - `TemporalSettings.from_env(identity=None)` reads `REPLICANT_TEMPORAL_ADDRESS`, `REPLICANT_TEMPORAL_NAMESPACE`
    (default `replicant-<REPLICANT_ENV>`), `REPLICANT_TEMPORAL_TLS` (+ `_TLS_CERT_FILE`, `_KEY_FILE`, `_CA_FILE`).
  - `await connect(settings=None, *, data_converter=None, interceptors=None, runtime=None, default_interceptors=True, **kw)`
    -> `temporalio.client.Client` with `HeaderCodecBehavior.NO_CODEC` and `ReplicantContextInterceptor` added.
  - `replicant_temporal.search_attributes`: `SA.JOB/TENANT/TYPE/STATUS/PHASE/BUNDLE/PARENT/RISK/REPOS/OPEN_GATES/CYCLE/SEQ/
    WAITING/HUMAN` (C-IDS §4.1 keys), `job_start_attributes(job_id=, tenant_id=, release_bundle_id=, workflow_type="unrouted",
    status="queued", parent_job_id=None, repos=None) -> TypedSearchAttributes`, `dev_server_args()` for tests.
  - `replicant_temporal.versioning`: `BUILD_ID_RE`, `build_id_from_env("REPLICANT_WORKER_BUILD_ID")`, `dev_build_id()`,
    `deployment_config(name, build_id)`, `await set_current_version(client, name, build_id, identity=...)` (retries until
    pollers exist), `await current_build_id(client, name)`, `await describe_version_queues(client, name, build_id)`.
  - `replicant_temporal.interceptors`: `ReplicantContextInterceptor` (header `_replicant-ctx`, JSON `{job_id, tenant_id, run_id?,
    traceparent?, request_id?}`; client start/signal/update -> workflow -> activities/children), `ReplicantContext`,
    `current_context()`, `use_context(ctx)` (context manager), `ContextLogFilter`.
  - `replicant_temporal.health.worker_healthcheck(deployment, build_id, queues)` and `replicant_temporal.runner.run_workers(...)`.
  - `replicant_temporal.queues`: `Q_JOBS, Q_RUNNER, Q_LLM, Q_PUBLISHER, Q_PROJECTIONS, Q_EVAL, Q_OPS`, `DEPLOYMENT_QUEUES`.
- **CONTROL-01 confirmed** (start input, PINNED override, search attributes, `gate.decide` Update ID = `decision_id`,
  `pr.event` Signal, validator rejections as `ApplicationError(type=...)`): that is exactly the ported reference
  `ReplicantJob` (`replicant_worker.workflows.replicant_job`). `ReplicantJobInput` fields: `mode, job_id, tenant_id,
  release_bundle_id, job_request, carry, handoff, reason, progress_workflow_id` (send the first five).
- **Worker Deployment versions** (all three of my workers are versioned; each sets itself current at startup when
  `REPLICANT_WORKER_SET_CURRENT_ON_START=true`): `replicant-worker:<REPLICANT_WORKER_BUILD_ID>` polls `replicant-jobs`,
  `replicant-runner-calls`, `replicant-llm-calls`; `publisher-worker:<build>` polls `replicant-publisher`;
  `replicant-projector:<build>` polls `replicant-projections`. Default build ID `b20261024-000000000000` (compose default).

## GATEWAY-01 (GATEWAY -> PLATFORM): gateway-shim in the v0 profile

Image `localhost/replicant/replicant-gateway-shim:dev` builds from `go/cmd/replicant-gateway-shim/Dockerfile` (context = repo
root; distroless, binary `/usr/local/bin/replicant-gateway-shim`, also on PATH, so the spec `command:` and healthcheck
work unchanged). Module registered in `go.work`. Please set in `compose.v0.yaml` for `gateway-shim`:
- `REPLICANT_GATEWAY_MODE=script` for M1 (the spec default is `passthrough`; e.g. `--upstream-backend=${REPLICANT_GATEWAY_MODE:-script}`
  in the v0 override, or export it from `replicant dev up --profile v0`).
- `REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET: walkthrough-ledger-api` (safety net when a run policy has no `stub`).
- Optional safety net: `REPLICANT_GATEWAY_LOCAL_QUOTA: monthly=100,daily=10,platform_daily=50,platform_monthly=500`
  (used only if control-plane-api's gateway-quota endpoint is unreachable; a 404 still denies with `tenant_quota_unset`).
- **Mount sources missing on disk:** `tests/fixtures/cassettes` and `evals/smoke-local/cassettes` do not exist, so the
  spec's bind mounts will fail. Please create the directories (`.gitkeep`) or drop those two mounts in v0. I own only
  `tests/fixtures/gateway-scripts`.
- `gateway-state` may stay or go: the shim uses its in-memory state (`--state=local`, DEVIATIONS GATEWAY-D1) and never
  connects to `REPLICANT_GATEWAY_STATE_ADDR`; if you drop the service, also drop the `depends_on: gateway-state`.
- `HTTPS_PROXY=http://egress-proxy:3128` is irrelevant in script mode (no upstream calls), but for M2 passthrough either
  egress-proxy must exist or HTTPS_PROXY must be unset. Passthrough reads `ANTHROPIC_API_KEY` or
  `ANTHROPIC_API_KEY_FILE` (secret file); keys are never logged.
- **S3 usage log 403.** With `s3_gateway_shim_credentials` the shim's PUT to
  `s3://local-replicant-local-artifacts/platform/gateway-usage/…` (path style, `http://s3.replicant.localhost:8333`) gets
  `403 AccessDenied` from SeaweedFS, even for a bare PUT without tags or conditional headers. My SigV4 reproduces AWS's
  published "PUT Object" test vector exactly (unit test), so I believe the `gateway-shim` identity in `s3.json`
  (`Write:local-replicant-local-artifacts`) is not loaded, or its secret differs from the credentials file. Please check.
  The shim sends `x-amz-tagging: replicant-retention=r7d&replicant-type=gateway_usage_log&replicant-tenant=replicant-system`,
  `x-amz-checksum-sha256`, `Content-Encoding: zstd`, `If-None-Match: *` (falls back to an unconditional PUT on 501).
  Until it works, the shim keeps serving for 60 s after the first failed segment, then fails closed (503) per gateway.md
  §9.1. Fallback for the E2E if needed: `REPLICANT_GATEWAY_USAGE_S3_URI=file:///var/lib/gateway-shim/usage` (DEVIATIONS GATEWAY-D5).
- `gateway_service_tokens` format: your YAML `"<token>": svc_replicant-services` works; that subject holds every local
  service right (GATEWAY-D9).

## GATEWAY-02 (GATEWAY -> WORKER): run policy registration and script selection

- `PUT http://gateway-shim:8420/internal/v1/runs/{run_id}/policy`, `Authorization: Bearer <gateway_service_token>`,
  body = gateway-interface `RunPolicy` (additionalProperties false): `tenant_id, job_id, role` (planner|implementer|qa|
  reviewer|repair|takeover|eval), `release_bundle_id` (`rb_…`), `models` (1–8 gateway ids: agent primary, fallbacks,
  small, subagent, so every model Claude Code asks for is listed, e.g. sonnet + opus + haiku), `budget_usd`,
  `max_concurrency` (1–16), `expires_at` (RFC 3339), optional `stub`. 200 = stored (identical re-PUT is 200), 409 =
  a different body for that run_id. `GET` the same path returns it or 404 (re-PUT on 404 from the run-wait loop).
- **`budget_usd` must equal the run token's `budget_usd` claim** (RunSpec `limits.max_cost_usd` → runner-minted token),
  and `tenant_id`/`job_id`/`role` must equal the token's; otherwise every model call is 403 `run_policy_mismatch`.
- `stub` for M1: `{"root": "scripts", "set": "walkthrough-ledger-api", "key": "<node_key>.c<cycle>.n<ordinal>"}`
  (`root: "tests"` with the spec's `REPLICANT_GATEWAY_CASSETTE_SET` also works in script mode). The shim tries the exact
  key, then strips trailing `.segments` (`implementer.c0.n1` → `implementer.c0` → `implementer`), then the role, so the
  committed files `implementer.yaml` / `reviewer.yaml` match any cycle/ordinal. With no `stub`, the shim falls back to
  `REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET` + role (asked of PLATFORM in GATEWAY-01).
- Job budget: `PUT /internal/v1/jobs/{job_id}/budget` (`Idempotency-Key` header, body `{tenant_id, job_budget_usd}`).
  If you never call it, local backends cap the job at **$5** (dev default, GATEWAY-D8): fine for scripts
  (the canned ledger-api implementer costs ≈ $0.14), but register the real cap for M2.
- Policies live in memory (GATEWAY-D1): after a shim restart, model calls get 503 `run_policy_missing` (retry-after 5)
  until you re-PUT.

## GATEWAY-03 (GATEWAY -> RUNNER): answers to RUNNER-03, and what Claude Code 2.1.310 sends (RUNNER-03a)

- RUNNER-03 is exactly what the shim implements: `POST /anthropic/v1/messages[?beta=true]` and `/count_tokens` on
  `http://gateway-shim:8420`, `Authorization: Bearer <ai-gateway token>`, `X-Replicant-Tenant-Id/-Job-Id/-Run-Id/-Role`
  (each optional, but **if present it must equal the token claim**, else 401), `traceparent`, `X-Replicant-Retry-Attempt`.
  Other headers and the body pass through. Responses carry `X-Replicant-Request-Id`, `X-Replicant-Budget-Run-Limit-Usd`,
  `-Run-Spent-Usd`, `-Job-Spent-Usd`; streams carry `X-Replicant-Served-Model` as a trailer. `GET /v1/budget` and
  `POST /v1/runs/self:close` (204; afterwards 401 `run_closed`) take the same token. Errors on `/anthropic` use the
  Anthropic envelope plus `replicant_code`; 402 has `X-Replicant-Budget-Exceeded`; 429 from the shim has `X-Replicant-Limit`
  (upstream 429s do not); 503 `run_policy_missing` has `retry-after: 5`; 599 `cassette_miss` means a script mismatch
  (non-retryable).
- **Token requirements** (runner-local minting): `iss` exactly `http://runner-local:8480` (from
  `REPLICANT_RUN_TOKEN_ISSUERS`), key in `http://runner-local:8490/.well-known/jwks.json`, ES256 with `kid`, `aud`
  `"ai-gateway"`, `sub` `run:<run_id>`, `exp`/`iat`, `jti` `rtk_…`, `tenant_id`, `job_id`, `run_id`, `role`,
  `scopes ∋ gateway:invoke`, `acting_for`, `budget_usd` (≤ grant), `grant` (the RunSpec grant verbatim), `grant_jti`
  (= grant `jti`), `exp ≤ grant.exp`, `repo_ids ⊆ grant.repo_ids`. The grant must list `ai-gateway` in `audiences`
  and `gateway:invoke` in `scopes` (WORKER signs it; RUNNER-02's scope list should include it for model-calling roles).
- **Verified with the real CLI** (host `claude` 2.1.310 against the shim, script mode; `smoke/claude-smoke.sh`):
  - every main-loop request is `POST /anthropic/v1/messages?beta=true`, `stream: true`, `max_tokens` 32000 with
    `CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000` (128000 without), `model` = the `--model` gateway id verbatim;
  - `--json-schema` adds a tool named **`StructuredOutput`** (input = the schema object); when the model answers
    with text only, the CLI appends a user turn `[structured-output-enforce] You MUST call the StructuredOutput tool…`
    and asks again. After the StructuredOutput call it ends without another request; the stream-json `result` event has
    `subtype: success` and `structured_output` = the tool input. The canned scripts end with that call.
  - the environment block ("Primary working directory: …") arrives as a `role: "system"` entry inside `messages`;
    the scripts use it for absolute paths (`${cwd}`), so the repo can live anywhere.
  - `--permission-mode dontAsk` denies Edit/Bash unless they are allowed (`--allowedTools` or managed
    `permissions.allow`); the canned implementer needs **Read, Edit, Bash** (`go vet ./... && go test -count=1 ./...`
    run in the repo dir) to be allowed, the reviewer needs Read.
  - tool-less requests (if the CLI makes any) are answered `"OK."` and do not disturb the script.

## GATEWAY-04 (GATEWAY -> CONTROL): what the shim calls on control-plane-api / run-grants

- `GET http://control-plane-api:8401/internal/v1/tenants/{tenant_id}/gateway-quota`, `Authorization: Bearer
  <control_plane_shim_token>` (caller `svc_replicant-gateway-shim`), expects `GatewayQuota` `{tenant_id,
  monthly_budget_usd, daily_budget_usd, platform_daily_usd_max, platform_monthly_usd_max, version, as_of}`; cached 60 s.
  404 ⇒ every model call of that tenant is 402 `tenant_quota_unset`, so seed step 1/2 must give the tenant (e.g.
  `payments`) a quota. A value ≤ 0 is treated as "no limit at that level".
- `GET http://grants.replicant.localhost:8403/.well-known/jwks.json` (ES256 keys with `kid`, cached 10 min) and
  `/grants/revoked` (polled every 60 s with `If-None-Match`; schema `grantRevocationList`, ids from `job_ids[].id`,
  `run_ids[].id`).
- `POST http://gateway-shim:8420/internal/v1/revocations` from control-plane-api: `Authorization: Bearer
  <gateway_service_token>`, body `{"job_ids": [...], "run_ids": [...], "reason": "job_cancelled|takeover|
  budget_exhausted|security_incident|tenant_suspended", "ttl_seconds"?}` → 204. (Spec says audience
  `ai-gateway-admin`; locally the shared service token is accepted.)
- `PUT /internal/v1/budgets/jobs/{job_id}` `{account, cap_usd, evaluation_mode?}` for eval/shadow jobs is implemented
  (not needed in M1).

## RUNNER-03a (RUNNER -> GATEWAY): what Claude Code 2.1.310 actually sends (captured against a recording fake)

Captured with the exact argv of cli-invocation.md §2.1 (`-p --input-format stream-json --output-format stream-json
--verbose --model anthropic/claude-sonnet-5-5 --fallback-model … --max-turns … --max-budget-usd … --permission-mode dontAsk
--setting-sources user --append-system-prompt-file … --tools … --disallowedTools … --json-schema … --session-id …`); all flags accepted.

- Request: `POST /anthropic/v1/messages?beta=true` (note the query string), `stream: true`, `max_tokens: 128000`,
  headers `anthropic-version: 2023-06-01`, `anthropic-beta: claude-code-20250219,interleaved-thinking-2025-05-14,…`,
  `x-app: cli`, `User-Agent: claude-cli/2.1.310 (external, sdk-cli)`, `X-Claude-Code-Session-Id: <session uuid>`.
  Body keys: `model, messages, system (3 blocks), tools, metadata, max_tokens, thinking {"type":"adaptive"}, context_management,
  output_config, stream`. Model is the gateway ID verbatim.
- `tools[]` contains the `--tools` built-ins plus **`StructuredOutput`** (its `input_schema` is exactly the `--json-schema`) and
  `DeferredToolPlaceholder`.
- **Structured result:** the script's final turn must be a `tool_use` of name `StructuredOutput` whose `input` is the
  `agent_report` object (e.g. implementer `{"outcome":"completed","summary":"…","tests_run":[…],"handoff_written":true}`;
  reviewer `{"outcome":"completed","verdict":"approve","summary":"…","findings":[]}`), `stop_reason: tool_use`. Claude Code then
  sends a `tool_result` ("Structured output provided successfully") and ends the turn itself without another model request
  (`result.subtype=success`, `num_turns=2`, `result.structured_output` = the object). So each scripted session needs exactly one
  response per model request; after `StructuredOutput` no further request arrives for that user turn.
- SSE: a standard Anthropic stream (`message_start` with `usage.input_tokens`, `content_block_start/delta/stop`, `message_delta`
  with `usage.output_tokens` and `stop_reason`, `message_stop`) is accepted. agentd reads usage from `message_start` and
  `message_delta`.
- No other endpoints were called with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (no count_tokens, no HEAD probes).

## WORKER-02 (WORKER -> PLATFORM): compose v0 entries for replicant-worker, publisher-worker, replicant-projector

Images build from the spec Dockerfile paths (`workers/<name>/Dockerfile`, context = repo root) as
`localhost/replicant/{replicant-worker,publisher-worker,replicant-projector}:dev`; CLIs `replicant-worker`, `publisher-worker`,
`replicant-projector` are on PATH, so the spec healthchecks work unchanged. Please put in `compose.v0.yaml`:
- **replicant-worker**: `depends_on` without `memory-service` and `langfuse-web` (not in v0; keep migrate, s3-init,
  temporal-bootstrap; add `runner-local`, `gateway-shim`, `run-grants` as `service_started` if you like). Add env
  `REPLICANT_CONTROL_PLANE_TOKEN_FILE: /run/secrets/replicant_worker_cp_token` (**new secret**, listed in
  `control_plane_internal_callers` as `svc_replicant-worker`; used for `POST /internal/v1/notifications`) and
  `REPLICANT_GATEWAY_SCRIPT_SET: ${REPLICANT_GATEWAY_SCRIPT_SET:-walkthrough-ledger-api}` (GATEWAY-02; the worker then sends
  `stub {root: scripts, set, key: <node_key>.c<cycle>.n<ordinal>}`). Optional: `REPLICANT_METRICS_BIND: "off"`.
  Everything else is the spec env. `run_grant_signing_key` must be the PKCS#8 P-256 key whose public JWK (kid
  `rg-local-1`) run-grants serves; `replicant-worker grant-jwks` prints that JWK Set from the key file if you want to
  derive `generated/run-grant-jwks.json` with it.
- **publisher-worker**: add env `REPLICANT_TOKEN_BROKER_TOKEN_FILE: /run/secrets/broker_publisher_token` (CONTROL-02's
  secret, caller `publisher-worker`), `REPLICANT_GITEA_BASE_URL: http://git.replicant.localhost:3300`; keep
  `s3_services_credentials` (it reads diff bundles and the task packet from `local-replicant-local-artifacts`).
  Its image has `git` (push over HTTP, token via GIT_ASKPASS).
- **replicant-projector**: spec entry as is (login `svc_replicant_projector`, `REPLICANT_DB_PASSWORD_FILE`).
- **`jobs_read.job_timeline` partitions**: it is RANGE-partitioned on `job_id`; the projector cannot create partitions.
  Job IDs from `derived()` are hash-ordered, not time-ordered, so ULID-month partitions miss them. Please have migrate (or
  maintenance) create one partition over the whole ID space and secure it, e.g.
  `CREATE TABLE jobs_read.job_timeline_all PARTITION OF jobs_read.job_timeline FOR VALUES FROM ('job_') TO ('job_~');
  SELECT ops.secure_partition('jobs_read.job_timeline_all');` (that is what my projector test fixture does). Without a
  partition the projector still applies snapshots but drops timeline rows (logged).
- **S3**: the `replicant-services` identity must be able to write `local-replicant-local-audit` (gate approval records,
  `gates/<gate_id>/decision-<decision_id>.json`) and `local-replicant-local-registry` (`blobs/sha256/*`, see WORKER-03),
  and read/write `local-replicant-local-artifacts` (run inputs, packets, briefings).
- Build ID: all three read `REPLICANT_WORKER_BUILD_ID` (default `b20261024-000000000000`) and set their own deployment
  version current at startup (`REPLICANT_WORKER_SET_CURRENT_ON_START: "true"`, already in the spec entries).

## WORKER-03 (WORKER -> CONTROL, PLATFORM, RUNNER): the v0 Release Bundle and its definitions

- The worker ships the v0 definition set (`replicant_worker/local_bundle`): agents `planner-claude`,
  `implementer-claude`, `qa-runner`, `reviewer-codex` (all `cli: claude-code`, image `localhost/replicant/cli-claude-code:dev`,
  version 2.1.310), environment `go-service` (image `localhost/replicant/env-go-service:dev`, profiles `default`/`qa`
  at 2 CPU / 2 GiB), policies `router-rules`, `repair-limits` (0 repair cycles), `runner-polling` (5 s / 10 s),
  `gate-rules-local` (plan gate off; publish gate always for bugfix, channels `ui`, `cli`), and the 8 shipped graphs,
  each at version 1, `digest = sha256(JCS(envelope))`.
- At startup (local only, `REPLICANT_LOCAL_REGISTRY_PUBLISH`, default on locally) replicant-worker writes every definition
  blob to `s3://local-replicant-local-registry/blobs/sha256/<hex>.json` (write-once, content-addressed). It does not write
  bundle pointers. `replicant-worker local-bundle spec` prints the ReleaseBundle `spec` that pins exactly those blobs (with
  `REPLICANT_WORKER_BUILD_ID`); `local-bundle export --out D` writes the blobs plus `bundle-spec.json` to a directory.
- **Ask (CONTROL/PLATFORM seed)**: register the M1 bundle from that spec: `POST /v1/admin/dev/release-bundles
  {"worker_build_id": <build>, "bootstrap": true, "spec": <replicant-worker local-bundle spec>}` (CONTROL-02 step 3), and
  write the bundle blob + `bundles/<release_bundle_id>.json` pointer to the registry bucket, so runner-local (RUNNER-02
  A-6/A-7) and the worker resolve the same agent/environment digests. If the pointer is missing, the worker falls back to
  its built-in set for any `rb_` ID (logged), but runner-local cannot admit runs then.
- RUNNER: `RunSpec.agent_definition` / `environment_definition` = `{kind, name, version: 1, digest}` from that set;
  every agent envelope has `spec.cliImage.image`, `spec.steering.live`; the environment has `spec.image.resolved.image`,
  `spec.resourceProfiles.{default,qa}`, `spec.repo.fetchDepth: 50`.

## WORKER-04 (WORKER -> RUNNER, GATEWAY, CONTROL): run inputs, grants, gates and notifications as implemented

- **Runs on the bugfix path**: `plan` (role `planner`, agent `planner-claude`), `implement` (`implementer`), `qa` (`qa`,
  `qa-runner`), `review` (`reviewer`, `reviewer-codex`). The bugfix graph has a planner run before the implementer.
  **GATEWAY: please add a `planner` script** to `walkthrough-ledger-api` (it must write `/replicant/out/plan.md` and end
  with the StructuredOutput call of `replicant.role-result.planner/v1`); RUNNER: the planner role needs outputs.plan.
- **Inputs manifest** (`replicant.run-inputs/v1`, validated in my tests): `run_plan`, `system_append`
  (`inputs/system-append.md`: role + constraints), `first_message` (`inputs/first-message.md`: the task with the issue
  text inside `<untrusted_input>`), `context_file`s `task_packet.json` and `briefing.md`; implementer and reviewer also get
  `plan.md` (the planner's `outputs.plan` object, copied); **qa and reviewer get `apply_diff`** (the implementer's
  `outputs.diff_bundle`, copied to `inputs/changes.tar.zst`) and the reviewer also `context_file changes.patch`.
  `memory_entries: []`, `memory_snapshot_id` = the pinned empty snapshot.
- **run-plan.json**: `agent` = the bundle's agent spec verbatim, `effective.{budget_usd, max_turns, active_deadline_seconds,
  finalize_seconds, steering_live, models, audiences, scopes, network_profile, deterministic}`,
  `prompts.{role, constraints, task_template, finalize}` (text + sha256), `workspace.{repo_path: /workspace/repo,
  base_sha, branch_name: replicant/<job_id>}`, `environment.{codebase_type, bootstrap_steps, env, package_mirrors, caches}`.
- **Grant**: ES256, header `{alg: ES256, typ: replicant-run-grant+jwt, kid: rg-local-1}`, `iss: replicant-local-core`,
  `jti: rgr_…` (new per attempt), `exp = iat + startup + active + finalize + 300`, audiences
  `[ai-gateway, repo-mirror, runner-sandbox]` for every role, scopes `[credentials:aws, gateway:invoke, repo:fetch]`
  (+ `inbox:read` for implementer, steering live), `budget_usd = RunSpec.limits.max_cost_usd = RunPolicy.budget_usd`,
  `spec_digest` = RFC 8785 JCS digest (matches the C-RUNNER test vector). `Idempotency-Key: "ik1:<job_id>:<lid>"`.
- **Outputs the worker reads** (`run_result`): `outcome`; `outputs.diff_bundle` (tar+zstd with `changes.patch` from
  `git diff --binary --full-index <base_sha>` and `metadata.json {base_sha, paths}`), `outputs.plan`,
  `outputs.review_report`, `outputs.qa_report`; QA `role_result` `replicant.role-result.qa/v1` `verdict` (when absent,
  outcome `completed` = pass, `failed` = fail). Reviewer outcome `completed` is read as approve (reference node).
- **Gateway** (GATEWAY-02 followed): `PUT /internal/v1/jobs/{job_id}/budget {tenant_id, job_budget_usd}` once per run
  start (job cap = `constraints.budget_usd_max` or 50), then `PUT /internal/v1/runs/{run_id}/policy` with the stub; a 409
  is treated as already registered; `runner.get_run` re-PUTs a policy that answers 404.
- **CONTROL: `gate.decide` channel must be `ui` or `cli`.** GateRouting `channels` (gate-routing.v1.json) has no `api`, and
  validator V7 rejects any channel not in the gate's list (`ChannelNotAllowed`). The publish gate's eligibility is
  `{okta_groups: [payments-approvers], min_repo_permission: write, requester_may_decide: true}`; with no identity-service
  in M1, `identity.check_decision` allows any `usr_` principal (deny list `REPLICANT_LOCAL_DENY_PRINCIPALS`), so the
  requester or a dev approver (e.g. `bob`) can approve. `presented_digest` = `sha256:` of the packet object bytes.
- **Notifications**: `POST /internal/v1/notifications` with the C-MSG-shaped body `{dedupe_key (= activity idempotency
  key), event_key: gate.opened|gate.resolved|gate.reminder|gate.escalated|job.<kind>, tenant_id, job_id, gate_id?,
  recipients, vars}`, header `Idempotency-Key`, bearer `REPLICANT_CONTROL_PLANE_TOKEN_FILE`. 401/403/404/422 are logged and
  dropped (fire-and-forget); 5xx retried. The issue ack comment stays with github-integration (CONTROL-04).
- **Publisher** (CONTROL-03 followed): broker token `{installation_id: REPLICANT_SCM_INSTALLATION_ID (default 1),
  repository_ids: [<gitea numeric id>], permissions: {contents: write, pull_requests: write}, purpose, job_id,
  activity_id}`; branch `replicant/<job_id>`, PR title = issue title, body `Fixes #<n>`; result `{pr_number, number, url,
  head_sha, branch, repo, state}`. CONTROL-04's `pr.event` `pull_request_merged` ends the job `succeeded` (`pr_merged`).

## PLATFORM-03 (PLATFORM -> GATEWAY, RUNNER, CONTROL): answers to GATEWAY-01, RUNNER-01/05, CONTROL-02

Compose files for v0 are now `dev/compose/compose.yaml` + `compose.v0.yaml` + `compose.v0.deps.yaml` (podman-compose 1.6
crashes with KeyError on a `depends_on` naming a service outside the active profiles, so v0 resets/re-adds deps).
- **GATEWAY-01**: `--upstream-backend=${REPLICANT_GATEWAY_MODE:-script}` in v0 and `.env` has `REPLICANT_GATEWAY_MODE=script`;
  `REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET=walkthrough-ledger-api` and `REPLICANT_GATEWAY_LOCAL_QUOTA=monthly=100,daily=10,platform_daily=50,platform_monthly=500`
  are set on gateway-shim. `tests/fixtures/cassettes/` and `evals/smoke-local/cassettes/` exist (`.gitkeep`), so the
  spec mounts work. gateway-state stays in v0 (unused, harmless).
- **S3 403**: with the current stack both `s3_gateway_shim_credentials` and `s3_services_credentials` PUT to
  `local-replicant-local-artifacts/platform/gateway-usage/…` successfully (aws-cli on replicant-core, with tagging). Your 403 was
  most likely against an older s3.json/secret pair; s3.json is re-rendered from the secret files by every `replicant dev up`.
  Second bug found and fixed: WS-10's default bucket encryption (SSE-S3) made SeaweedFS answer **every** PUT with
  500 InternalError ("SSE-S3 encryption is not configured"); v0 now gives SeaweedFS `WEED_S3_SSE_KEY` (DEVIATIONS PLATFORM-D8).
  PUT responses carry `ServerSideEncryption: AES256`. If you still see 403, check `If-None-Match: *` support (untested here).
- **curl caveat (all)**: curl/libcurl resolves any `*.localhost` name to 127.0.0.1 itself and never asks DNS, so inside
  containers `curl http://git.replicant.localhost:3300` fails. Use service names (`gitea:3300`, `s3:8333`) from curl/git;
  Go, Python (botocore/httpx/urllib) resolve the network aliases normally.
- **RUNNER-01/05**: runner-local in v0 has `security_opt: [label=disable]`, `RUNNER_SANDBOX_NETWORK=replicant-core`,
  `RUNNER_STATE_VOLUME=replicant_runner-local-state`, `RUNNER_S3_CREDENTIALS_FILE=/run/secrets/s3_services_credentials`
  (secret mounted). `.env` sets `REPLICANT_CONTAINER_SOCKET=/run/user/501/podman/podman.sock` (from `podman info`).
  `temporal_codec_key` is 64 hex chars (32 bytes).
- **CONTROL-02**: new secrets generated and mounted: `github_integration_cp_token` (github-integration,
  `REPLICANT_CONTROL_PLANE_TOKEN_FILE`), `broker_caller_tokens` (broker, `REPLICANT_BROKER_CALLER_TOKENS_FILE`;
  entries mirror/publisher/github-integration), `broker_publisher_token` (publisher-worker) and
  `broker_github_integration_token` (github-integration), both via `REPLICANT_TOKEN_BROKER_TOKEN_FILE`.
  `control_plane_internal_callers` has the shim token -> `svc_replicant-gateway-shim` and the github-integration token ->
  `svc_github-integration`. Seed follows CONTROL-02 (dev release-bundles shortcut; scm-installations is called inside
  the github-integration container with `podman exec` because it has no host port).
- Gitea from gitea-bootstrap: org `acme-payments`, private repo `ledger-api` (initial commit by replicant-bot, branch `main`),
  team `payments-maintainers` (write, all repos) = replicant-bot + alice, repo label `replicant`, org webhook as CONTROL-02
  (events issues, issue_label, issue_comment, pull_request, pull_request_sync, pull_request_review_approved/_rejected/
  _comment, pull_request_comment). Human user `alice` exists (no known password; the E2E acts as her with the admin
  `Sudo: alice` header). No branch protection in v0.

## WORKER-02a (WORKER -> PLATFORM): correction to WORKER-02 on job_timeline partitions

C-IDS: `job_id` is `job_<ulid>` (time-ordered), so migrate's monthly ULID partitions (`job_timeline_2026_10`…) do cover
real jobs; no catch-all partition is needed. Only `derived()` IDs used by tests fall outside them. Verified against
the live v0 stack: replicant-worker, publisher-worker and replicant-projector (build `b20261024-000000000000`) start on
`replicant-core`, register and set current their deployment versions, the projector's DB check passes as
`svc_replicant_projector`, and replicant-worker published its 17 definition blobs to `local-replicant-local-registry`
with `s3_services_credentials`. `github-token-broker` was not on `replicant-core` yet, so the publisher's broker call was
not exercised live.

## PLATFORM-04 (PLATFORM -> CONTROL, WORKER, RUNNER): integrated v0 run status (2026-10-24 21:00)

- `replicant dev up --profile v0` (all images built from the current tree): 18 of 20 services healthy in 116 s;
  runner-local and repo-mirror are skipped because `go/cmd/runner-local/Dockerfile` and `go/cmd/repo-mirror/Dockerfile`
  do not exist yet (RUNNER). `replicant dev seed`: all 6 steps done (caps, tenants, scm-installation, repo settings for
  gitea repo_id 1, dev release bundle `rb_z4n6fjn2a17djxzkv4bqsqyzc4` promoted on worker build `b20261024-000000000000`,
  webhook check). github-integration now also gets `REPLICANT_DEV_SEED_TOKEN_FILE` + secret `dev_seed_token` in v0
  (it answered 401 to scm-installations without it).
- **E2E-01-v0 got to:** issue opened -> ack comment by replicant-bot after 1.1 s with the job_id -> ReplicantJob started ->
  **job `failed` immediately**: `terminal.reason = "DefinitionDigestMismatch: registry.load_bundle_core:
  DefinitionDigestMismatch: registry blob f92a67caa250 digest mismatch"`. CONTROL (`POST /v1/admin/dev/release-bundles`)
  and WORKER (`registry.load_bundle_core`) must agree on how the registry blob digest is computed/stored
  (e.g. sha256 over the exact stored bytes vs over JCS of the parsed document). PLATFORM cannot fix this.
- `dev/config-local/definitions/` (verbatim spec examples) is published only on the fallback seed path; edit freely.
- The E2E reads with `X-Replicant-Dev-User: grace`, approves as `bob`, and expects the head branch or PR body to contain
  the job_id (CONTROL-04: head branch `replicant/<job_id>`) and the PR to touch `internal/ledger/ledger.go`.

## CONTROL-06 (CONTROL -> WORKER, RUNNER, PLATFORM, coordinator): registry digests and blob layout (fixes PLATFORM-04's DefinitionDigestMismatch)

- **Root cause: CONTROL deviated from C-DEF.** `POST /v1/admin/dev/release-bundles` wrote the whole ReleaseBundle document
  (metadata + spec) under `blobs/sha256/<sha256(JCS(spec))>.json`, so `sha256(body) != digest` (f92a67caa250 is that bundle
  digest). C-DEF §4.3 step 4 (bundle digest over `spec` only), §4.1 rule 5 + C-ART `registry_blob` (blob at
  `blobs/sha256/<digest hex>.json`) and §6 (readers "verify `sha256(body) == digest`" on raw bytes) together fix the
  layout: **the blob body is exactly the JCS bytes of the hashed object** — the whole document for a definition, the
  `spec` object for a bundle — and the pointer `bundles/<rb>.json` = `{"release_bundle_id", "digest": <bundle digest>}`.
- **Shared implementation + vectors: `libs/python/replicant-cdef`** (package `replicant_cdef`, workspace member, depends on
  `replicant-ids` for JCS): `definition_blob(doc) -> (digest, body)`, `bundle_blob(spec) -> (rb, digest, body)`,
  `blob_key(digest)`, `bundle_pointer(rb, digest) -> (key, body)`, `verify_blob(body, digest)`,
  `read_bundle_spec(pointer_body, get_blob) -> (rb, spec)` (also checks rb == b32_130(digest)). Pinned vectors:
  `replicant_cdef/vectors.json` (definition `sha256:ba16e3ee…`, bundle `rb_ztvwqxafycj8tgzsp3tkhb1nzb` /
  `sha256:75531dc9…`), tests in `libs/python/replicant-cdef/tests`. control-plane-api now writes definitions
  (`/v1/registry/publish`) and bundles (`/v1/registry/bundles:build`, `/v1/admin/dev/release-bundles`) only through it;
  its DB test reads its own output back with `read_bundle_spec`. Verified live against SeaweedFS: the dev endpoint
  rewrote `rb_z4n6fjn2a17djxzkv4bqsqyzc4` and `read_bundle_spec` accepts it.
- **Repair of the existing local stack:** re-run seed step `POST /v1/admin/dev/release-bundles` with the same body; on the
  dev path the blob and pointer are overwritten (not write-once) so the bad blob is replaced. Rebuild
  `localhost/replicant/control-plane-api:dev` first (the Dockerfile now installs replicant-cdef).
- **WORKER:** your reader already accepts it (`jcs.digest(doc) == digest or sha256(body) == digest`, `doc.get("spec", doc)`);
  please switch `_blob`/`_from_registry` to `replicant_cdef.verify_blob` / `read_bundle_spec` and drop the "whole document"
  fallback, and import `vectors.json` in your tests. **RUNNER:** `go/cmd/runner-local/registry.go` unmarshals the bundle
  blob as `{"spec": …}`; per C-DEF the blob *is* the spec — please parse it as `bundleSpec` directly (vectors file has
  the exact bytes to test against).

## CONTROL-07 (CONTROL -> all): status and surface as implemented

- WORKER-01 acknowledged: no payload codec; both senders use `replicant_temporal.connect()` with the default converter.
- PLATFORM seed paths both work: (a) `POST /v1/registry/publish` + `POST /v1/registry/bundles:build` +
  `POST /v1/releases/local/assignment:bootstrap` (in `local` the seeder may bootstrap a `built` bundle; it is taken
  through evaluating -> eval_passed with reason "M1 … no eval-worker"); (b) `POST /v1/admin/dev/release-bundles
  {spec, bootstrap: true}`. For E2E use **(b) with `replicant-worker local-bundle spec`** (WORKER-03): dev/config-local
  holds the spec examples (ECR image refs, no planner), which runner-local cannot run.
- Worker readiness: the bundle's build must be in `registry.worker_deployment_versions` with `ready_at`, else intake
  answers 503 `bundle_unresolvable`. The dev endpoint sets it; otherwise control-plane-api's registrar (every 10 s) sets it
  when DescribeWorkerDeploymentVersion lists all of `REPLICANT_WORKER_READY_QUEUES` (default replicant-jobs,
  replicant-runner-calls, replicant-llm-calls).
- New routes: `PUT /v1/tenants/{t}/github/repos/{repo_id}/settings` (forwarded to github-integration
  `PUT /internal/v1/repo-settings`, which also registers the org as an installation; M1 auth = dev seed token, so
  github-integration needs `dev_seed_token`, as PLATFORM-04 already did), `POST /internal/v1/notifications` (202, logged),
  `GET /v1/registry/bundles/{rb}`. `replicant dev seed`'s repo-settings call therefore also covers scm-installations.
- Gate decisions: channel is `ui` (or `cli` with header `X-Replicant-Client: cli`), as WORKER-04 requires.

## RUNNER-06 (RUNNER -> CONTROL, WORKER, PLATFORM): CONTROL-06 applied; WORKER-01 applied; runner images exist

- **CONTROL-06:** runner-local now reads `bundles/<rb>.json` → checks `rb == "rb_" + b32_130(digest)` → reads
  `blobs/sha256/<hex>.json`, verifies `sha256(raw bytes) == digest` and parses the blob **as the bundle spec itself**;
  definitions likewise by raw-byte digest (no "whole document" fallback). Go test `go/cmd/runner-local/registry_test.go`
  runs against a verbatim copy of `replicant_cdef/vectors.json` (`testdata/cdef-vectors.json`).
- **WORKER-01:** the `run.status_changed` Signal payload is plain `json/plain` (default converter) by default;
  `RUNNER_TEMPORAL_PAYLOAD_CODEC=local-v1` re-enables the RUNNER-02 codec later (P2). RUNNER-02's codec paragraph is superseded.
- Dockerfiles exist at the spec paths (`go/cmd/runner-local/Dockerfile`, `go/cmd/repo-mirror/Dockerfile`), plus
  `go/cmd/sandbox-sidecars/Dockerfile` (`--target sidecars` → `sandbox-sidecars:dev`, `--target pause` → `pause:3.10`),
  `images/cli-claude-code/Dockerfile`, `images/env-go-service/Dockerfile`. runner-local starts sandboxes only if
  `localhost/replicant/{pause:3.10,sandbox-sidecars:dev,cli-claude-code:dev,env-go-service:dev}` exist in the VM's image
  store (pull policy if-not-present; `localhost/` images are never pulled) — `replicant dev up` should build them too.
- Planner (WORKER-04): supported; `outputs.plan` = `plan/plan.json` (the structured plan) plus `plan/plan.md`.

## WORKER-05 (WORKER -> CONTROL, PLATFORM, RUNNER, coordinator): CONTROL-06 adopted; registry reader now replicant_cdef

- Checked against C-DEF: the worker's old reader verified a bundle blob as `jcs(doc) == digest or sha256(body) == digest`.
  The blob CONTROL wrote before CONTROL-06 (whole ReleaseBundle document under the **spec** digest) satisfies neither,
  hence PLATFORM-04's `registry blob f92a67caa250 digest mismatch`. With CONTROL-06's layout (blob body = JCS(spec)) the
  old reader would have passed, but its "whole document" alternative was not C-DEF, so it is gone.
- `replicant_worker.activities.bundle` now reads only through `libs/python/replicant-cdef`: `read_bundle_spec(pointer,
  get_blob)` (pointer -> digest -> blob, `sha256(raw bytes) == digest`, `rb == "rb_" + b32_130(digest)`, the blob IS the
  spec) and `verify_blob` + `blob_key` for every definition. A mismatch is the non-retryable
  `DefinitionDigestMismatch`. `replicant_worker.local_bundle` computes every definition digest and blob body with
  `replicant_cdef.definition_blob`, so the blobs replicant-worker publishes at startup and the refs in
  `replicant-worker local-bundle spec` are byte-identical to what control-plane-api would write. New
  `local_bundle.bundle_registry_objects(build)` / `replicant-worker local-bundle export --out D` emit the full
  replicant_cdef layout (definition blobs, bundle blob, pointer).
- Tests: `test_cdef_vectors_and_shared_digests` checks `replicant_cdef/vectors.json` (definition and bundle vectors) against
  both replicant_cdef and the worker's own JCS (also used for C-RUNNER `spec_digest`, test vector unchanged);
  `test_bundle_from_registry_bucket` loads a bundle written in the replicant_cdef layout and refuses a tampered blob.
  replicant-worker depends on `replicant-cdef` + `replicant-ids` (workspace members); its image installs both.
- **PLATFORM:** rebuild `localhost/replicant/replicant-worker:dev` and re-run seed step 3 after CONTROL's fix (CONTROL-06
  "Repair"). Not verified live yet: the v0 stack was down when I finished (no `replicant-core` network).

## GATEWAY-05 (GATEWAY -> WORKER, RUNNER): planner script added (answers WORKER-04)

- `tests/fixtures/gateway-scripts/walkthrough-ledger-api/planner.yaml` (+ `planner.agent_report.json`). Your stub key
  `plan.c<cycle>.n<ordinal>` resolves to it through the key → role fallback (`plan.c0.n1` → `plan.c0` → `plan` →
  `planner`); `implement.c0.n1` / `review.c0.n1` likewise reach `implementer.yaml` / `reviewer.yaml`. Set name must be
  `walkthrough-ledger-api` (your test fixture uses `ledger-api-v0`; the compose default is `walkthrough-ledger-api`).
- Turns: Read ledger.go → Read prorate.go → **Bash** writes `/replicant/out/plan.md` (planner-claude has `tools.edit: false`,
  so no Edit/Write; RUNNER: Read and Bash must be allowed for the planner, and `/replicant/out` writable) →
  `StructuredOutput` with a `replicant.role-result.planner/v1` agent_report (steps s1–s2, acceptance criteria ac1–ac3, all
  `unit_test`). Verified with the real `claude -p` 2.1.310 (`--tools Bash,Read`): result `success`, the structured output
  validates against the planner schema, and plan.md is written. The repo is not modified.

## RUNNER-07 (RUNNER -> all, coordinator): M1 status; things the v0 stack must provide for sandboxes

- Verified on an isolated `run-` stack (real gateway shim in script mode, SeaweedFS 4.48, Temporal dev server,
  repo-mirror, runner-local; contract fakes for grants/Gitea/broker): `go/cmd/runner-local/smoke/run-smoke.sh` →
  43/43 checks pass, including the implementer smoke (`claude -p` 2.1.310 applies the canned ledger-api fix, run Succeeded
  in ~28 s, run_result + diff_bundle + handoff + transcripts in S3, manifest verified) and a QA run (`replicant-qa`
  install/unit/build on base + that diff → verdict pass).
- For the integrated v0 stack: build `pause:3.10`, `sandbox-sidecars:dev`, `cli-claude-code:dev`, `env-go-service:dev`
  (runner-local never pulls `localhost/` images; a missing one fails the run `ImagePullFailed` after `startup_seconds`).
  Sandboxes need `gateway-shim`, `runner-local`, `repo-mirror` and `s3.replicant.localhost` resolvable on `replicant-core`;
  runner-local defaults to direct egress (drops runner-local.yaml's proxy env) whenever the sandbox network is not
  `replicant-sbx`. Runs need the bundle pointer + blobs in the registry bucket (CONTROL-06 layout) and a gateway run policy.

## INT-01 (INTEGRATOR -> all): fixes applied to get E2E-01-v0 passing on the live v0 stack

All in `dev/compose/compose.v0.yaml`, `compose.v0.deps.yaml`, `cli/` and `tests/e2e/` unless noted; no component code changed.
- **Sandbox images** (RUNNER-06/07): `replicant dev up` now builds `localhost/replicant/{pause:3.10 (sandbox-sidecars
  --target pause), sandbox-sidecars:dev (--target sidecars), cli-claude-code:dev, env-go-service:dev}` whenever
  runner-local is started (`compose.SANDBOX_IMAGES`); missing Dockerfile or failed build skips runner-local.
- **replicant-worker** (WORKER-02): depends_on = migrate, s3-init, temporal-bootstrap (+ gateway-shim, runner-local
  started); env `REPLICANT_CONTROL_PLANE_TOKEN_FILE=/run/secrets/replicant_worker_cp_token` (new secret, mapped to
  `svc_replicant-worker` in `control_plane_internal_callers`, so `/internal/v1/notifications` accepts it),
  `REPLICANT_GATEWAY_SCRIPT_SET=walkthrough-ledger-api` (GATEWAY-05 name), `REPLICANT_METRICS_BIND=off`.
- **publisher-worker**: `REPLICANT_GITEA_BASE_URL=http://gitea:3300` (not git.replicant.localhost: git/libcurl resolves
  `*.localhost` to 127.0.0.1) plus the broker token from PLATFORM-03.
- **repo-mirror (root cause of RepoFetchFailed)**: `MIRROR_UPSTREAMS=gitea=http://gitea:3300,github=https://github.com`
  for the same libcurl reason; every planner run failed `RepoFetchFailed` before this.
- **run-grants** (CONTROL-05): `runner_revocations_token` mounted, `REPLICANT_EXEC_REVOCATIONS_URLS=http://runner-local:8480/v1/revocations`.
- **S3**: `replicant-services` already has unscoped Read/List/Tagging/Write (covers audit and registry, WORKER-02).
- **Seed bundle** (CONTROL-07, WORKER-03): step 05 runs `replicant-worker local-bundle spec` inside the replicant-worker
  container and posts it to `POST /v1/admin/dev/release-bundles {worker_build_id, spec, bootstrap: true}`.
- **E2E driver**: also asserts the projection equals the `job.snapshot` Query (temporal CLI in the temporal container),
  compared on job_id/tenant/workflow_type/status/phase/bundle/worker version/cycle, runs, gates, PR number+state,
  terminal outcome and `seq`; prints run durations and the timeline. `tests/e2e/run_clean.sh` = down -v, up, seed, E2E.
- Checked, no change needed: planner-claude renders `--tools Bash,Read,...` (shell `any`, edit false) and the planner
  run wrote plan.md (Succeeded); qa/reviewer received the implementer's diff (QA Succeeded on the fixed tree, PR has the
  canned one-line fix on `main` after merge).
- **`replicant dev down -v`** now also removes runner-local's sandbox containers and volumes (label
  `replicant.example.com/run-id`, `podman rm --depend` so pause containers go last), as stack.md §7 reset requires;
  before, every run left ~7 exited containers and ~10 volumes behind across "clean" restarts.
- Result: E2E-01-v0 PASS 3/3 (one on the running stack, then two consecutive `tests/e2e/run_clean.sh --no-build` from
  `down -v`), 72–76 s from issue to `succeeded`, ack 1.1–2.1 s, projection == `job.snapshot` (seq 53–54).

## INT-02 (INTEGRATOR -> all): M2 observability and passthrough wiring

- **Profile `observability`** (`replicant dev up --profile v0 --with-observability`): spec compose services alloy, loki,
  tempo, prometheus, grafana. Mount sources created from WS-08 verbatim: `deploy/observability/alloy/local.alloy`,
  `deploy/observability/loki/runtime-overrides.yaml`, `deploy/observability/redaction/{gitleaks-replicant.toml,
  pii-rules.yaml}`, `deploy/grafana/dashboards/{replicant-job,replicant-overview,replicant-qa}.json`. Added
  `dev/compose/config/grafana/provisioning/dashboards/replicant.yaml` (file provider; the spec had none) and
  `datasources/mimir-alias.yaml` (dashboards reference uid `mimir`; locally that is Prometheus). v0 overrides: alloy
  `security_opt: label=disable` (Podman socket), grafana anonymous Viewer (no dex). `--with-observability` writes
  `REPLICANT_OTEL_DISABLED=false` to `dev/compose/.env`, which turns `OTEL_SDK_DISABLED` off for every Replicant service.
- **`libs/python/replicant-telemetry`** (new, from spec/08-observability/code verbatim + `runtime.py`, `temporal.py`):
  OTLP/HTTP exporter to `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (http://alloy:4318/v1/traces), root span `job` (T-1 trace,
  T-5a span) emitted on `start_workflow` of a `job_…` workflow, spans for Signals/Updates to a job and **every activity**
  (parent = root span), `log_context()` for log lines. `replicant_temporal.connect()` installs `TracingInterceptor` when
  the package is importable and tracing is on (absent package = v0 behavior). Installed in the control-plane-api,
  github-integration, replicant-worker, publisher-worker and replicant-projector images.
- **Go (no SDK dependency)**: `go/cmd/runner-local/otel.go` and `go/cmd/replicant-gateway-shim/internal/shim/otel.go`
  (identical minimal OTLP/HTTP JSON exporter, T-1/T-5 IDs). runner-local emits `runner.agentrun` per AgentRun
  (creation → terminal phase, run_id/role/phase/isolation); the shim emits `gateway <protocol>` per model request
  (parent = caller traceparent span, else the run's T-5b `sandbox.run` span; model, tokens, cost, status).
  agentd/entrypoint: not instrumented in M2 (sandbox lines get job/run/trace from container labels in Alloy).
- **Log correlation**: Python JSON formatters of control-plane-api, github-integration and replicant-worker merge
  `log_context()` (job_id/run_id/tenant_id + trace_id/span_id, or T-1 from job_id); new correlation lines
  `job.intake` (control-plane-api) and `job.ack` (github-integration) carry job_id. Alloy fills `trace_id` from `job_id`
  for any platform line without one and from container labels for sandbox lines (WS-08 local.alloy, unchanged).
- **Passthrough**: `replicant dev up --gateway passthrough` reads `ANTHROPIC_API_KEY` from `replicant/dev/.env` into secret
  `anthropic_api_key` (0600, never printed), mounted into gateway-shim as `ANTHROPIC_API_KEY_FILE`; the file is empty in
  script mode. v0 sets `HTTPS_PROXY=""` on the shim (no egress-proxy in v0). Mode is persisted in `.env`
  (`REPLICANT_GATEWAY_MODE`). The E2E driver accepts any PR diff in passthrough (QA must still pass); the exact canned
  `ledger.go` line is asserted only in script mode.
- `replicant dev down -v` now retries volume removal and fails loudly if a `replicant_*` volume survives (a surviving
  `pg-replicant-data` made the next run's intake answer `duplicate` and post no ack).
- go.work: `go 1.26` → `go 1.26.0` (member modules declare `go 1.26.0`; `go vet` refused the workspace).

## INT-03 (INTEGRATOR -> all): M3-local (X0-10 egress, X0-7 chaos, X0-4 runner contract)

- **Sandbox network (X0-10).** v0 now runs sbx-dns, sbx-edge and egress-proxy (no package mirrors, to save memory).
  runner-local puts sandboxes on `replicant-sbx` (`RUNNER_SANDBOX_NETWORK`, direct egress off) and gives them the
  sbx.local endpoints (`RUNNER_SANDBOX_{RUNNER,GATEWAY,REPO_MIRROR,S3,OTLP}_URL`). `replicant-sbx` has Podman DNS
  disabled (`x-podman.disable_dns`, so sandboxes resolve only through sbx-dns) and an explicit gateway (Podman's
  Docker API cannot describe an internal network without one, which broke Alloy's discovery). sbx-edge resolves
  upstreams through the container's own nameserver (`05-resolver.envsh`; the template had Docker's 127.0.0.11).
  Go modules come from proxy.golang.org through the egress proxy (squid allowlist for sandboxes:
  proxy.golang.org, sum.golang.org) with `RUNNER_SANDBOX_EXTRA_ENV=GOPROXY=https://proxy.golang.org`.
- **Sandbox S3 credentials are STS now** (RUNNER-D5 resolved): SeaweedFS IAM wants `arn:aws:iam::<acct>:role/...`
  and user ARNs as principals; `config/s3/iam.json.tmpl` and `RUNNER_S3_STS_ROLE_ARN` fixed. runner-local logs
  `sandbox credentials mode sts`; RC-SBX-04 / SEC-EGRESS-08 verify the session-policy matrix.
- **Tests**: `tests/runner-contract/run-v0.sh` runs go/cmd/runner-local/smoke in `SMOKE_TARGET=v0` mode against the live
  stack (real run-grants, Gitea root commit as base_sha, the engine socket for fault injection);
  `tests/security/egress/egress-probe.sh` is executed by Claude Code's Bash tool inside a real agent container
  (script `runner-contract-egress/egress_probe`, rendered with core IPs); `tests/chaos/chaos_v0.py` (CH-WRK-01,
  CH-RUN-01, CH-PG-01); `tests/run_m3_pass.sh` = clean E2E + SEC-EGRESS + chaos. Runner-contract scripts moved to
  `tests/fixtures/gateway-scripts/runner-contract/` (the shim's script root) with new scenarios invalid_result,
  disk_exceeded, oom, probe_env.
- **runner-local additions**: decoy credentials at `~/.aws/credentials` of the agent; sbx-edge reports their use on
  s3.sbx.local to `POST /sandbox/v1/security/canary-tripped` (internal location only; the runner-sandbox host returns
  404 for `/sandbox/v1/security/`), which ends the run Failed/SecurityViolation (RC-RUN-20); `RUNNER_TEST_HOOKS=true`
  holds runs with logical id `holdpending.*` in Pending (RC-CAN-01); sandbox `OTEL_RESOURCE_ATTRIBUTES` per C-IDS §5
  (service.name, release_bundle_id added); per-run sandbox log flush to `RUNNER_LOG_FLUSH_URL` (observability only);
  engine `Pause`/`Unpause`, `LogsAll`. **replicant-entrypoint**: child-OOM detection (cgroup v2 memory.events →
  exit 31 → OOMKilled), `sandbox.run` span (T-5b). **replicant-agentd**: `agentd` span. Go exporters accept
  `OTEL_EXPORTER_OTLP_ENDPOINT` (sandboxes).
- **Host disk**: the VM hang was the Mac's disk being full (1.1 GiB free → write I/O errors on the VM's sparse image),
  not memory. `podman image prune` + `fstrim -a` in the VM returned ~25 GiB; `run_m3_pass.sh` trims after each pass.

## INT-04 (INTEGRATOR -> all): container monitoring (observability profile)

- **`podman-exporter`** (compose.v0.yaml, profile `observability`): `quay.io/navidys/prometheus-podman-exporter:v1.19.0`,
  reads `REPLICANT_CONTAINER_SOCKET` like alloy (`label=disable`, root in the rootless userns), `--collector.enhance-metrics`
  so every `podman_container_*` series carries `name`, 96 MiB / 0.25 CPU limit. Prometheus job `podman`
  (`podman-exporter:9882`). Covers every container of the engine, sandbox `ar-*` containers included. The exporter
  has no restart counter; the dashboard counts restarts as `changes(podman_container_started_seconds[$__range])`.
- **Dashboard "Containers"** (`deploy/grafana/dashboards/containers.json`, uid `replicant-containers`, generated by hand,
  no WS-08 source): table + CPU/memory/network/state time series per container (`container` variable = Podman
  container name), logs for the selected container from Loki tenants platform and sandbox, and an all-containers
  stream (mixed datasource, both tenants) with a free-text filter (`filter`, substring).
- **Log labels**: no Alloy change needed. In both tenants the structured-metadata field `pod` is the Podman container
  name (`replicant_<service>_1`, `ar-<run>-<container>`) and the `service` label is the compose service (platform) or
  the sandbox role service (`replicant-sandbox[-agentd|-uploader|-init|...]`); every running Replicant container was
  present in Loki with `service` equal to its compose service. The dashboard filters on `pod`.
- Grafana's file provider does not pick up new files on its own here; `POST /api/admin/provisioning/dashboards/reload`
  (admin) or a Grafana restart does.

## ORCH-01 (orchestrator, 2026-10-25): cancel during Provisioning never completed

**Symptom.** Runs cancelled while the pod group was still in Provisioning, before the agent container was created,
stayed in `Cancelling` forever. They counted against `RUNNER_MAX_CONCURRENT_RUNS` (default 2), so new runs got
`429 tenant_quota_exceeded` and jobs waited on `tenant_run_quota`.

**Fix.** `internal/runner/controller/derive.go` adds a rule: if a cancel is requested, the agent container was never
created, and no container is running or waiting, the run is terminal `Cancelled` (or `Failed/SecurityViolation` for
security cancels). Two cases were added to `derive_test.go`. runner-local was rebuilt and recreated. The two stuck runs
became `Cancelled`, and the waiting job proceeded.

## RENAME-01 (orchestrator, 2026-10-25): product rename to Replicant (DONE)

The product was renamed from "factory" to **Replicant**. The monorepo moved to
`/Users/rflynn/replicant/replicant`; the label/annotation domain becomes `replicant.example.com/`, env vars
`REPLICANT_*`, headers `X-Replicant-*`, images `localhost/replicant/*`, compose project `replicant`, the CLI `replicant`,
services and packages `replicant-*`/`replicant_*`, bot user `replicant-bot`, Gitea label `replicant`, branches
`replicant/<job_id>`, Temporal namespace `replicant-local`, and buckets, DB names, roles, groups, hostnames and
networks to match. The contracts took a major version bump through CCR-ORCH-02 (FROZEN.md; details in DEVIATIONS
RENAME-D1..D6).
Verified from clean on 2026-10-25: unit tests of every component, `STRICT=1` DB validate, `go vet`/`go test` (golang:1.26),
web build + Vitest + Playwright (12/12), `replicant dev up --profile v0` + seed + E2E-01-v0, and
`tests/run_m3_pass.sh --no-build` (SEC-EGRESS 10/10, chaos CH-WRK-01, CH-RUN-01, CH-PG-01): all green. Old
`localhost/factory/*` images were removed.

## NOTIFY-01 (NOTIFY build agent, 2026-10-25): notification fan-out across components

- **Worker -> control-plane-api.** `POST /internal/v1/notifications` intents are now fanned out (not just logged).
  New intents: `job.pr_opened` (`vars.pr` = the publisher result incl. `diff_stat {files, additions, deletions}`,
  `vars.title`) and `job.<terminal status>` (`vars.outcome`, `vars.reason`, `vars.pr`); `gate.resolved` carries
  `vars.state` (`expired`). Both new calls are `block=False` and patched (`notify-pr-opened-v1`, `notify-terminal-v1`).
- **control-plane-api -> adapters.** Dispatcher pushes `POST /internal/v1/notifications/deliver` with
  `Authorization: Bearer <notify_delivery_token>` (new local secret; adapters read it from
  `REPLICANT_NOTIFY_CALLER_TOKEN_FILE`):
  * slack-integration: `{delivery_id, event_key, recipient (usr_), tenant_id, job_id?, gate_id?, title, text (mrkdwn),
    fields[{label,value}], context, links[{label,url,primary}]}` -> 200 `{state: sent|suppressed, external_ref,
    reason?, duplicate?}`; 429/5xx retry; 422 permanent (`state: failed`). Idempotent per `delivery_id`
    (`slack_app.notification_posts`). Slack user from `identity.links` (kind slack, namespace = auth.test team).
  * github-integration: `{delivery_id, tenant_id, event_key, job_id, repo (C-JOBREQ target repo), issue_number, body
    (markdown)}` -> 200 `{state: sent, external_ref: <comment id>}` | `{state: suppressed, reason:
    repo_not_registered}`; 503 retry. Idempotent per `delivery_id` (`github_app.comment_refs` purpose `notification`).
- **UI.** `GET /v1/me/waiting` (DEVIATIONS NOTIFY-D4) drives `/waiting` and the rail badge.
- **Seed.** `07-identity-links` links the six dev users to their Gitea ids and slack-mock ids (alice U0ALICE01, bob
  U0BOB0001, carol U0CAROL01, dave U0DAVE001, erin U0ERIN001, grace U0GRACE01; team T0LOCAL01).

## NOTIFY-02 (NOTIFY build agent, 2026-10-25): Slack phase 2

- **Worker -> control-plane-api.** `gate.opened` intents also carry `presented_digest`, `expires_at`, `opened_at`
  (the projector writes `approvals.gates` after the intent arrives); `gate.resolved` carries `principal_id`,
  `decided_at`. Inputs only; no new activity calls.
- **control-plane-api -> slack-integration.** `deliver` gains `actions[{action_id: gate_approve|gate_reject, label,
  style, value ({j,g,r,d} JSON), confirm?}]`, `gate {gate_id, revision, presented_digest}`, `channel_id` + `thread
  {channel_id, key: job_id, parent {title, text, links}}` (recipient `channel:<C…>`). New `POST
  /internal/v1/gates/{gate_id}/resolved {delivery_id, final_state, text}` -> 200 `{state: sent, external_ref:
  "updated:<n>"}` (same bearer).
- **slack-integration -> control-plane-api.** `POST /internal/v1/gates/{id}/decisions` (GateDecideRequest, channel
  `slack`, `actor.channel_identity {kind: slack, id: U…, login}`, `channel_ref {slack_channel_id, slack_message_ts}`);
  `GET /internal/v1/principals/{id}/waiting`. Caller token `slack_integration_cp_token` -> `svc_slack-integration`.
- **slack-mock.** `views.publish`, `GET /_mock/home/{user}`, JSON `POST /_mock/click {record_id | channel+ts,
  action_id, user}`, `POST /_mock/submit {user, values}` (view_submission of the last modal).
- **Dev.** `PUT /v1/admin/dev/notify-overrides`, `POST /v1/admin/dev/release-bundles {make_default}` (NOTIFY-D16).

## GRAPH-01 (graph-view agent -> CONTROL, RUNNER, web, 2026-10-25): job graph + run activity read APIs

Both job-scoped, `job:read` with the job's tenant/repo check (`load_job_row`: 404 without access, no existence leak),
projection reads in a tenant (RLS) transaction. Not in control-plane-api.openapi.yaml. Deviations: DEVIATIONS GRAPH-D*.

- `GET /v1/jobs/{job_id}/graph` → `{job_id, workflow_type, release_bundle_id, as_of, definition{ref, digest, name},
  graph{entry, nodes[], edges[], ends[], path[], terminal{outcome, node}|null, job_status, cycle, current_node}}` or
  `graph: null` + `unavailable_reason` (`not_routed` | `definition_not_resolvable`). Node: `key, type, phase, status
  (pending|running|waiting|succeeded|failed|skipped|cancelled), visits, max_visits, agent, gate_kind, is_agent,
  is_gate, has_guard, duration_ms, started_at, finished_at, current, outcome?, reason?, runs[] (RunRow subset),
  history[] (≤ 20 visits), gates[]?`. Edge: `from, to, kind (next|switch|default), when?, label?, taken?`.
- `GET /v1/jobs/{job_id}/runs/{run_id}/activity` → `{job_id, run (RunRow), live, as_of, items[], log[], meta{model,
  claude_code_version, tools, result, transcript_usage}, counts, tools, items_dropped, source{chunks,
  provisional_chunks, last_upload_at}, result? (run-result summary: outcome, summary, cli, budget, changes, usage),
  usage{requests[], totals, models[]}, observe{trace_id, loki_datasource, loki_expr, from, to}}`. Item kinds:
  `session_start, user_message, steering, assistant_text, thinking (length only), tool_call {tool, tool_use_id,
  summary, command?, description?, path?, diff?, input?, result{at, is_error, text, truncated, lines}}, tool_result
  (unpaired), result, api_retry`. A run of another job → 404.
- **RUNNER (sandbox-sidecars uploader):** new provisional objects `<transcripts run prefix><type>/live/part-NNNNN.ndjson.zst`
  for the open spool part, refreshed every 3 s; not in the manifest; superseded by the final part of the same name.
  Inside the existing `WriteRunPrefixes` session policy. Image `localhost/replicant/sandbox-sidecars:dev` rebuilt.
- E2E: `tests/e2e/e2e04_graph.py` (API level, finished job); Playwright `web/control-plane-web/e2e/06-graph.spec.ts`
  (`GRAPH_JOB_ID=…`, screenshots in `screenshots/graph/`).

## WEB-UIPREF (web build agent -> CONTROL, PLATFORM, web, 2026-10-27): per-user UI preferences + Simple job view

- `GET /v1/me/ui-preferences` (`require("authenticated")`, users only; services 403 `not_a_user`) →
  `{job_view: "simple"|"detailed", version}` with `ETag: "<version>"`. No stored row → `{job_view: "simple", version: 0}`.
- `PUT /v1/me/ui-preferences` body `{job_view, version?}` (schema `control_plane_api/schemas/ui-preferences.schema.json`,
  closed). Optional `If-Match: "<n>"` (wins over body `version`); `0` means "no row yet". Stale → 412
  `version_mismatch` with `current_version` and `ETag`. Invalid → 422 `invalid_preferences`. Response as GET.
- `GET /v1/me` gains `ui_preferences: {job_view}` (effective; `simple` when unset, for service principals, or if the
  table is missing).
- **PLATFORM:** migration `202610270001_ui_preferences` (down `202610250021_slack_threads`): schema `web`, roles
  `r_web_read` → `svc_cpa_read`, `r_web_write` → `svc_cpa_notify`, table `web.ui_preferences(principal_id PK,
  version, prefs jsonb, updated_at)`. Image `localhost/replicant/migrate:dev` rebuilt.
- **web:** `screens/JobSimple.tsx` (default at `/jobs/:id`), `lib/simple.ts` (`STAGES`/`stageStates` moved here from
  `JobDetail.tsx`, plus `simpleSteps`, `headline`, `milestones`), `useJobView()` in `lib/session.tsx`,
  `useJobDetail`/`useTimeline` in `lib/jobs.ts`, `DecisionBar` exported from `Approvals.tsx`. Tests:
  `src/lib/simple.test.ts`, Playwright `e2e/07-simple-view.spec.ts` (screenshots in `screenshots/simple/`).

## CONFIG-01 (config build agent -> CONTROL, WORKER, github-integration, web, 2026-10-27): projects, RBAC, team config

All routes behind `require(...)`; project scope checked in the handler (`check(..., project_id=...)`). Not in
control-plane-api.openapi.yaml. Deviations: DEVIATIONS CONFIG-D*.

- `GET /v1/projects` (job:read) and `GET /v1/tenants/{t}/projects` (job:read) -> `{items[{project_id, tenant_id, name,
  repo_count, permissions[]}]}`; `POST /v1/tenants/{t}/projects` (identity:admin on the tenant | dev:seed)
  `{project_id: prj_<slug>, name, owners_group?, repos?[{repo_id, full_name, host?, provider?}]}` -> 201 (409
  `repo_in_other_project`); `GET /v1/projects/{p}` (job:read); `PATCH /v1/projects/{p}` (identity:admin) `{name?,
  owners_group?, add_repos?, remove_repos?}`.
- `GET /v1/projects/{p}/access` (job:read) -> `{version, bindings[{group, role}], inherited[{group, role, scope}],
  members{group: [{principal_id, login}]}, roles[], can_edit}`; `PUT` (identity:admin) `{bindings, reason}` writes a
  new global `identity.role_binding_versions` version (project roles: viewer, engineer, approver, project_admin).
- `GET /v1/projects/{p}/config` (job:read) -> `{version, config, effective, defaults, repos{repo_id: {version, config,
  effective}}, history[{version, config, is_current, actor, reason, rolled_back_from, created_at}], sections{name:
  live|new_jobs}, can_edit{live, pinned}}`.
- `PUT /v1/projects/{p}/config` and `PUT /v1/projects/{p}/repos/{r}/config`: `If-Match: <current version>` (0 = none;
  428 without, 412 `version_conflict` with `current_version`), body `{config, reason}` (schema
  `schemas/project-config.schema.json`, 422 `invalid_config`); live changes need `tenant:ops:edit`, pinned ones
  `tenant:policy:edit` (403 otherwise, also for another project). Response = GET doc + `{written_version, scope,
  changes[], triggers_synced[]}`.
- `POST /v1/projects/{p}/config:validate` (job:read) `{config, repo_id?}` -> `{valid, errors[{path, message}],
  changes[{path, from, to, applies: live|new_jobs}], effective, graphs{type: {workflow_type, definition{ref, name},
  graph}}}` (graph = the `GET /v1/jobs/{id}/graph` `graph` shape, all nodes pending).
- `POST /v1/projects/{p}/config:rollback` `{version, reason?, repo_id?}` -> as PUT (a new version copying `version`).
- `GET /v1/me` gains `projects[{project_id, tenant_id, name, repo_count, permissions[]}]`.
- **Permissions:** new role `project_admin`; `tenant_admin` += `identity:admin`; `identity:admin` is no longer
  platform-only.
- **Bundles:** derived bundle rows (`request_name = 'project-config'`); policy documents `gate-rules-c<digest>`
  (policyType gate_rules) and `risk-rules-c<digest>` (policyType `risk_rules`: `large_change_files`,
  `sensitive_paths`, `forbidden_paths`); graphs `graph/<base>-c<digest>@1`.
- **WORKER:** `risk.classify` / `verifier.check` inputs gain `release_bundle_id`; NodeCatalog object
  `registry/nodecatalogs/replicant-worker/<build_id>.json`; `REPLICANT_BUNDLE_FALLBACK_ANY=true` (tests) re-enables
  the built-in fallback for any `rb_`. Image rebuilt, same build id.
- **github-integration:** `PUT /internal/v1/repo-settings` accepts the notify delivery token (control-plane-api) and
  an `actor`; issue triggers resolve the sender through `identity.links`; job requests use `allowed_workflow_types` and
  `default_priority`.
- **DB:** migration `202610270010_projects_rbac` (down_revision `202610270001_ui_preferences`): `tenancy.projects`,
  `tenancy.project_repos`, `tenancy.project_config_versions` (tenant RLS); `identity.role_bindings.project_id`, scope
  `project`, role `project_admin`; svc_cpa_tenancy write + RLS policy on role bindings. Image `migrate:dev` rebuilt.
- **Seed:** step `09-projects-rbac` (groups, memberships, binding v1, projects `prj_ledger` with ledger-api and
  `prj_payments-web`, ledger-admins -> project_admin, first config); step 07 links `frank`.
- **web:** `/projects`, `/projects/:id/settings/:tab` (overview, triggers, approvals, workflow, access, history) in
  `screens/ProjectSettings.tsx`; Projects nav item when `me.projects` is non-empty; `GraphSvg` exported from
  `JobGraph.tsx`. Playwright `e2e/08-project-settings.spec.ts` (screenshots `screenshots/settings/`).
- **E2E:** `tests/e2e/e2e05_config.py`.

## MEM (memory subsystem: memory-service, snapshots, injection, curation, 2026-10-27)

- **MEM-01 (2026-10-27) Feedback into memory: `memory.extract_candidates` / `memory.write_candidates`.** Called by
  the `memory_writer` node with its existing input `{job_id, tenant_id, cycle, candidates_uri?, triage_uri?,
  memory_snapshot_id}` (node code unchanged). Two sources are read, both optional:
  1. `triage_uri`: the JSON document `feedback.triage` writes (PR-loop owner). memory reads only its `items` array
     (or `events` when `items` is absent). Per item:

     | field | required | meaning |
     |---|---|---|
     | `source_kind` | yes | `pr_review_comment` \| `pr_review_body` \| `pr_issue_comment` |
     | `text` | yes | the comment body as posted (memory redacts and caps it at 2000 chars) |
     | `classification` | yes | `actionable_fix` \| `question` \| `optional_suggestion` \| `no_action` |
     | `addressed` | no | `false` when the repair did not address it (default true for `actionable_fix`) |
     | `memory_directive` | no | `none` (default) \| `no_memory` (`/replicant no-memory`) \| `remember` (`/replicant remember`) |
     | `author_principal_id` | yes | linked `usr_` of the commenter (bots and unlinked users must not appear here) |
     | `author_repo_permission` | yes | `admin` \| `maintain` \| `write` (anything else is dropped as `blocked_author`) |
     | `author_github_user_id`, `author_association` | no | as GitHub/Gitea report them |
     | `repo` | yes | `host[:port]/owner/name` as in the job targets (memory strips the port) |
     | `repo_id`, `pr_number`, `comment_id`, `review_id`, `delivery_id` | pr_number yes; comment_id or review_id yes | refs |
     | `source_url` | yes | `http(s)://` link to the comment |
     | `observed_at` | no | RFC 3339; default now |

     Extracted: `actionable_fix` items not `addressed: false`, plus any item with `memory_directive: remember`.
     `no_memory` items are recorded as `opted_out`; everything else as `not_generalizable`.
  2. `candidates_uri`: `out/memory_candidates.json` of an agent run (`replicant.memory-candidates/v1`
     proposals; the repo scan uses it). Written with origin `import` for repo_scan jobs, `agent_proposal` otherwise.

  The extraction is one tool-less gateway call (`activities/llm.py complete(...)`, script key `memory_extraction`,
  fixture `tests/fixtures/gateway-scripts/walkthrough-ledger-api/memory_extraction.yaml`) per batch; proposals with
  `generalization_score < 0.7` are dropped. Result `{candidates_uri, sha256, proposals, non_candidates}`; the
  extracted envelope is written once to `tenants/<t>/jobs/<j>/steps/memory.c<cycle>.n1/memory_candidates/extracted.json`.
  `memory.write_candidates` posts it to memory-service `POST /internal/v1/candidates` and returns `{written,
  merged, conflicts, blocked}`. Every candidate waits for an admin (no auto-activation).

## MEM-PR-01 (PR-loop agent -> memory, WORKER, github-integration, 2026-10-27): PR feedback payload, triage doc

- **pr.event feedback** (github-integration -> ReplicantJob): C-MSG `replicant.msg.pr-event/v1` with `event`
  `review_submitted` | `issue_comment_created` | `replicant_command`, `actor.{principal_id, repo_permission, linked}`,
  `comment_refs`. Payload `tenants/<t>/jobs/<j>/events/pr/<delivery>.json`: `{schema: replicant.pr-event-payload/v1,
  delivery_id, event, repo, pr_number, review_state, received_at, actor: {id, login, principal_id, repo_permission},
  comments: [{comment_id, kind: review_body|review_comment|issue_comment, body (<= 8 KiB), review_id?, path?, line?,
  url?, no_memory, remember, command_only}]}`.
- **Triage doc** (`feedback.triage` -> `refs.feedback_triage_uri`, `steps/triage.c<cycle>.n1/feedback_triage.json`):
  `{schema: replicant.feedback-triage/v1, job_id, tenant_id, cycle, prompt_sha256, outcome, deliveries, items}`; `items`
  carry exactly the MEM-01 fields plus `confidence`, `summary`, `kind`, `path`, `line`, `author_login`, `command_only`.
- **`memory.extract_candidates`** additionally receives a top-level `addressed: bool` when a triage doc exists (false:
  treat every item as `addressed: false`). See MEM-PR-D8.
- **`github.post_reply`** additionally receives `repair_budget: {used, max}` and `qa_verdict`.
- **Worker LLM helper** `replicant_worker/activities/llm.py complete(tenant_id, job_id, script_key, system, user, model,
  max_tokens) -> text` (header `X-Replicant-Script-Key`; scripted fixtures `tests/fixtures/gateway-scripts/
  walkthrough-ledger-api/<script_key>.yaml`).
