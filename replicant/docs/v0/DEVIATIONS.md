
## GATEWAY (replicant-gateway-shim, tests/fixtures/gateway-scripts)

- **GATEWAY-D1 State backend: in-memory, not Valkey.** The shim runs with `--state=local` (the default in v0; the
  gateway.md Risks "local mode" store) instead of `gateway-state` Valkey. Single replica. Run policies, job budgets,
  budget accounts, counters, reservations, revocation sets and script cursors are lost on restart: the worker heals
  policies through 503 `run_policy_missing` + re-PUT (gateway.md §5.2), revocations are re-read within one feed poll,
  but **spent counters restart at zero** (no reseed from usage-ingestor, which is not in M1). `--state=valkey` refuses
  to start. The `gateway-state` service may stay in the profile; the shim ignores `REPLICANT_GATEWAY_STATE_ADDR`.
- **GATEWAY-D2 Backends.** `script` and `passthrough` only; `record` and `replay` refuse to start. `enterprise` reuses
  the passthrough adapter (route upstreams, key from `*_API_KEY[_FILE]`), no enterprise metadata header.
  Passthrough keys may also come from `<NAME>_FILE` (a compose secret) besides the env var.
- **GATEWAY-D3 Script semantics (replicant.gateway-script/v1 interpretation + extensions).** Turn selection is a per-run
  cursor over tool-carrying requests with retry idempotency by `messages` length; tool-less requests are answered
  `"OK."` without consuming a turn; a `fault` turn is consumed once. `structured_output_from` (and the added inline
  `structured_output`) is rendered as a `StructuredOutput` tool call (Claude Code `--json-schema`), followed by the
  added `finish` text if the CLI answers it. Added keys: `finish`, `expect.tools`, `respond.tool_uses` (list),
  `respond.stop_reason`, `${cwd}`/`${repo}` substitution. Lookup falls back from `<key>` through its dot-prefixes to
  `<role>`, root `tests` is also looked up under the `scripts` root in script mode, and
  `REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET` selects a set for runs without `stub`. Service callers select by purpose.
- **GATEWAY-D4 Tenant quota fallback.** `REPLICANT_GATEWAY_LOCAL_QUOTA` (local only, refused with the enterprise
  backend) supplies the quota when `REPLICANT_CONTROL_PLANE_URL` is empty or control-plane-api is unreachable / answers
  other than 200/404 with nothing cached. A 404 still means 402 `tenant_quota_unset`.
- **GATEWAY-D5 Usage log.** Same key layout and zstd ndjson segments (≤ 10 s or 1,000 events), but the sink can be a
  local directory (`file://` URI or `REPLICANT_GATEWAY_USAGE_LOG_DIR`). If the S3 server rejects `If-None-Match: *` with
  501/NotImplemented the shim retries unconditionally (SeaweedFS). No SSE-KMS headers unless
  `REPLICANT_GATEWAY_USAGE_SSE_KMS_KEY_ID` is set. The usage-ingestor fast path is best effort (queue of 10,000, drop
  oldest); the durable log is authoritative. Fail-closed (503) after 60 s of failed segment writes, as specified.
- **GATEWAY-D6 Not implemented in v0:** Prometheus metrics and OTel spans (one structured log line per request
  instead, never bodies); tenant RPM/TPM/concurrency limits (run concurrency is enforced); the unparsed-usage
  per-minute admission cap (settling at the estimate is implemented); Valkey pub/sub and leader lock; Bedrock and
  Vertex routes; embeddings in script mode; a child `traceparent` upstream (the incoming one is forwarded as is).
- **GATEWAY-D7 Error codes.** Request-validation failures on the internal API and malformed data-plane bodies use
  `replicant_code: invalid_request` (400), which is not in the ReplicantError enum. Script misses use `cassette_miss` (599).
- **GATEWAY-D8 Dev budget defaults.** With a non-enterprise backend, a job with no `PUT /internal/v1/jobs/{id}/budget`
  gets `budgets.dev_defaults.job_usd` ($5) as its job limit, and a run token without `budget_usd` gets `run_usd` ($2)
  (gateway.md §8 passthrough defaults, applied to script mode too).
- **GATEWAY-D9 Local service tokens.** `gateway_service_tokens` is a YAML map `token: subject`; subjects
  `svc_replicant-services` (PLATFORM's single shared `gateway_service_token`) and `svc_local-dev` hold every local
  service right (worker policy/budget, control-plane revocations/accounts, embeddings, eval judge). Prod uses projected
  ServiceAccount tokens per subject. Usage events record the purpose-appropriate principal.
- **GATEWAY-D10 Readiness.** `/readyz` does not require the JWKS to be fetched yet (they are fetched lazily on the
  first token), so the shim can be healthy before runner-local and run-grants (avoids a start-order cycle).

## WORKER (replicant-worker, publisher-worker, replicant-projector, libs/python/replicant-temporal)

- **WORKER-D1 Port layout.** `spec/01-orchestration/reference/replicant_orchestration` is ported to `replicant_worker`
  with the repo-layout subpackages (`workflows/`, `graphs/`, `adapters/langgraph/`, `nodes/`, `activities/`); imports are
  absolute, logic unchanged. Spec files the code and tests read are vendored unmodified under
  `replicant_worker/resources/spec/` (same relative paths). Workflow code has four additive, deterministic input
  fields (all from state already in history): `agents.build_run_plan` gets `base_sha` and `job_cap_usd`,
  `context.stage_inputs` gets `agent_definition`/`environment_definition` (the plan result's pinned refs),
  `agents.stage_inputs` gets `job_cap_usd`, and every `notify.*` input gets `tenant_id`. The 44 scenarios and the unit
  tests run against the ported package.
- **WORKER-D2 Bundle source.** No replicant-config repo, registry publish pipeline or Langfuse in M1: the worker ships the
  v0 definition set (`replicant_worker/local_bundle`, built from the spec examples) and resolves a bundle from the local
  registry bucket when `bundles/<rb>.json` exists, otherwise from the built-in set for any `rb_` ID (logged). Locally it
  writes the definition blobs to the registry bucket at startup (C-ART: only control-plane-api writes `registry`;
  content-addressed write-once, local only, `REPLICANT_LOCAL_REGISTRY_PUBLISH`). Prompts are inline v0 texts, not Langfuse
  versions; `spec.prompts` stays empty in the bundle spec.
- **WORKER-D3 Definitions.** `reviewer-codex` (name kept because the shipped bugfix.yaml references it) runs
  `cli: claude-code` in v0 (the local image has no Codex). `planner-claude` is derived from the implementer example (no
  example exists in WS-03). CLI and environment images are local tags (`localhost/replicant/...:dev`), not `@sha256`
  digests as the WS-03 schemas require. MCP servers removed (no memory-mcp/context-mcp in M1), so grant audiences are
  `ai-gateway, repo-mirror, runner-sandbox` and scopes `gateway:invoke, repo:fetch, credentials:aws` (+`inbox:read`).
  Environment resource profiles are laptop-sized (2 CPU / 2 GiB).
- **WORKER-D4 Router.** Deterministic rules only: `router_rules` gains rule `v0-issue-trigger-is-bugfix` (channel
  github, `issues.labeled|opened`) before `otherwise-ask-the-model`, and `llm_risk_review: false`. `router.parse_intent`
  fails non-retryably (`IntentParsingDisabled`), which the reference workflow treats as "model unavailable" and routes
  to the fallback type (bugfix); plan §1.2's "reject with a clarification comment" is not implemented in the worker.
- **WORKER-D5 Gates.** `gate_rules` stays in WS-01's fixture shape (WS-07's policy-gate-rules evaluator is not in M1)
  with local values: plan gate off (`enabled: false`), publish gate required for every bugfix, channels `ui`, `cli`
  (GateRouting has no `api` channel), `requester_may_decide: true`, no reminders/escalations. With no identity-service,
  `identity.check_decision` allows any `usr_` principal except `REPLICANT_LOCAL_DENY_PRINCIPALS`; `identity.check_action`
  likewise. `gates.resolve_routing` omits `on_contested_decision` (not in gate-routing.v1.json).
- **WORKER-D6 Budgets and cost.** No usage-ingestor: `budget.allocate_run` = min(agent limit, job cap) with nothing
  spent, `budget.tenant_quota_status` always available, `cost.record_run` logs usage only, `budget.apply_extension` is
  not in v0. The gateway job budget is registered with the job cap (`constraints.budget_usd_max`, else 50).
- **WORKER-D7 QA, verification, risk.** `qa.stage_contract_stubs` stages nothing (no ecosystem); `qa.classify` reads the
  QA run's `role_result` (or the run outcome when there is none) and writes `steps/<step>/qa_report/classified.json`;
  `qa.index_results` (on replicant-projector) is acknowledged without indexing (no `qa.test_results` producer and no S3
  read identity for the projector). `verifier.check` and `risk.classify` implement a v0 subset: forbidden/agent-config
  paths and CI paths, a regex secret scan of added lines (not `replicant-secretscan` with KMS fingerprints), >20 files ->
  medium; the verification report key is content-addressed (`verification-<sha16>.json`).
- **WORKER-D8 Run inputs.** `context.stage_inputs` (WS-04's activity) is implemented in the worker: briefing and task
  packet from the JobRequest (no memory, catalog or handoff), plan/diff staging per role (WORKER-04). Manifest
  `created_at` is the first attempt's wall clock; retries reuse the write-once manifest and its digest.
  `agents.stage_inputs` computes `expires_at` from the clock and treats a 409 as "already registered"; `runner.get_run`
  re-PUTs a policy the shim lost (GET 404).
- **WORKER-D9 Notifications.** `notify.*` post to `POST /internal/v1/notifications`; 401/403/404/422 and an unreachable
  control-plane-api (local only) are logged and dropped instead of retried, so a missing notification route never holds
  a job.
- **WORKER-D10 Publisher on Gitea.** The commit is built with `git apply --index --binary` and pushed with git over HTTP
  (token via GIT_ASKPASS) as `replicant-bot`, unsigned, instead of the GitHub Git Database API's App-signed commit;
  idempotency follows §10.1 (trailer check on `replicant/<job_id>`, PR lookup by head before create). Not in v0:
  CODEOWNERS reviewer requests, the `replicant:pr-open` label, the rebased diff of `check_base` (it reports
  `applies_cleanly: false` whenever the base moved), the run-manifest digest cross-check, `push_takeover_branch`.
  PR title/body come from the job's task packet (`steps/context.c0.n1/task_packet/task.json`) because the publisher has
  no intake read grant. Drafts use Gitea's `WIP:` title prefix.
- **WORKER-D11 Projector.** `jobs.repo_ids` (and `job_prs.repo_id`) come from `intake.job_requests.request.targets`
  (the JobSnapshot has no repo IDs); `job_prs.state` becomes `merged`/`closed` from the terminal outcome;
  `session_purge_due_at` = the `artifacts.expire_session_state` event time + 7 days; decisions are written with
  `text_key`/`text_sha256` only once both are known (CHECK `ck_gate_decisions_text_pair`); audit rows use channel
  `github` for `gitea`. A missing `job_timeline` partition skips the timeline row (logged) instead of failing.
- **WORKER-D12 Versioning.** All three deployments (`replicant-worker`, `publisher-worker`, `replicant-projector`) are
  versioned and set themselves current at startup when `REPLICANT_WORKER_SET_CURRENT_ON_START=true`; temporal-bootstrap
  does not do it. No payload codec (plan §1.3).

## PLATFORM (workspace, contracts, replicant-ids, replicant-db, dev/compose, CLI, e2e driver)

- **PLATFORM-D1 Temporal dev server.** v0 `temporal` is `temporal server start-dev` from `temporalio/admin-tools:1.32.1`
  (CLI 1.9.1, server 1.32.0, SQLite file in volume `temporal-dev-data`), not `temporalio/server:1.32.1` on
  `postgres-temporal` + `temporal-schema` (ADR-11-001). Same address `temporal:7233`, namespace `replicant-local`, the 14
  C-IDS search attributes (registered by the unmodified spec `bootstrap.sh`), and every key of
  `config/temporal/dynamicconfig.yaml` passed as `--dynamic-config-value`. Worker Deployment APIs answer
  (`temporal worker deployment list`, checked by `bootstrap-v0.sh`). Allowed by PLAN.md.
- **PLATFORM-D2 temporal-ui without OIDC and codec.** dex and temporal-codec-server are not in M1:
  `TEMPORAL_AUTH_ENABLED=false`, no codec endpoint. Anyone on 127.0.0.1:8233 can browse the namespace.
- **PLATFORM-D3 Owner DDL applied verbatim; drift check covers modelled schemas only.** Each of the 17 owner files is
  one Alembic revision that executes the reviewed `.sql` (shipped as package data) through the raw psycopg cursor; the
  spec's ORM models exist only for `ops`, so `alembic check` / `--verify-drift` compares `ops` only. Equivalence of all
  17 schemas with the spec DDL is proven instead by `libs/python/replicant-db/tools/validate_migrations.sh` (catalog diff,
  5,923 facts, plus grants/RLS/partition/eval-reader/audit-exporter probes, `STRICT=1` passes). Downgrades drop the
  objects each file creates (CI round trip only).
- **PLATFORM-D4 Local run metadata.** `ops.migration_runs` requires `sha256:<64 hex>` and a 40-hex git SHA; local images
  have no registry digest and compose passes a 12-hex SHA, so with `REPLICANT_ENV=local|ci` the CLI zero-fills both.
- **PLATFORM-D5 Maintenance after migrate (local/ci).** There is no maintenance CronJob locally, so `replicant-db migrate`
  runs `partition_create`, `tenant_partitions` and `partition_secure` under the same advisory lock right after a
  successful migration when `REPLICANT_ENV` is `local` or `ci` (`REPLICANT_MIGRATE_LOCAL_MAINTENANCE=false` disables it).
  Without it, partitioned tables (e.g. `jobs_read.job_timeline`) have no partition for the current month.
- **PLATFORM-D6 gitea-bootstrap does the Gitea part of seed step 4.** stack.md puts org/repo/webhook creation in
  `replicant dev seed`; the v0 brief puts it in gitea-bootstrap (`config/gitea/bootstrap-v0.sh`, after the unmodified spec
  `bootstrap.sh`): users gitea-admin, replicant-bot, alice (only); org acme-payments; team payments-maintainers (write);
  repo ledger-api only (pushed from `dev/sample-repos/ledger-api`, not a `.bundle`); repo label `replicant`; org webhook with
  the spec events plus `issue_label`. No branch protection (the E2E merges as alice/admin). `replicant dev seed` only checks it.
- **PLATFORM-D7 Sandbox network.** sbx-edge, sbx-dns, egress-proxy and the mirrors are not in M1; runner-local puts
  sandboxes on `replicant-core` (`RUNNER_SANDBOX_NETWORK`), so sandboxes reach every core service and the internet.
- **PLATFORM-D8 SeaweedFS SSE key.** s3-init applies WS-10's default bucket encryption (SSE-S3); SeaweedFS 4.48 then
  rejects every PUT with 500 unless it has a key, so v0 sets `WEED_S3_SSE_KEY` (random, from the generated `.env`).
- **PLATFORM-D9 Secrets location.** `REPLICANT_DEV_HOME` is `replicant/dev/.secrets` (compose `../.secrets`), not
  `dev/compose/.replicant-dev`; file names and `/run/secrets/<name>` paths are the spec's. v0 adds four secrets for
  CONTROL (`github_integration_cp_token`, `broker_caller_tokens`, `broker_publisher_token`,
  `broker_github_integration_token`) and an unmounted `s3_sts_signing_key` plus the inputs of rendered files.
- **PLATFORM-D10 Compose layering.** podman-compose 1.6 (the only compose on this machine) raises KeyError on a
  `depends_on` that names a service outside the active profiles (even with `required: false`), and its `!override`
  cannot express a nested mapping. v0 therefore uses `compose.v0.yaml` (resets those `depends_on`) plus
  `compose.v0.deps.yaml` (re-adds the v0 subset with the spec conditions). `compose.yaml` itself differs from the spec
  only by `v0` in 20 services' profiles. The spec `.env` (placeholder values) is not copied; the CLI generates it.
- **PLATFORM-D11 Seed (v0).** `replicant dev seed` runs 6 steps: platform caps, tenants (payments, local-dev,
  replicant-eval), SCM installation (github-integration `POST /v1/admin/scm-installations`, called inside its container
  because it has no host port), repo settings (`PUT /v1/tenants/payments/github/repos/<gitea id>/settings`, 404
  tolerated: repos register on the first webhook), the bundle via CONTROL's local-only `POST /v1/admin/dev/release-bundles`
  (falls back to publish + `bundles:build` + `assignment:bootstrap`), and the Gitea webhook/label check. Identity, role
  bindings, Slack, prompts, eval, ecosystem and memory steps are not in M1. `dev/config-local/definitions` are verbatim
  copies of spec examples (only used by the fallback path).
- **PLATFORM-D12 E2E humans.** The driver opens the issue as Gitea user alice through the admin `Sudo` header and acts
  in control-plane-api with CONTROL's local dev principals (`X-Replicant-Dev-User: bob` approves, `grace` reads). The
  "projection equals job.snapshot" assertion of X0-2 is not implemented (needs a Temporal query surface).
- **PLATFORM-D13 Sample repo is pushed from a directory.** `dev/sample-repos/ledger-api/` is a plain directory (not a
  `.bundle`); its `ISSUES/` folder is not pushed.

## CONTROL (control-plane-api, run-grants, github-integration, github-token-broker, libs/python/replicant-scm, replicant-cdef)

- **CONTROL-D1 Identity: no Dex, no identity-service.** Static dev directory in the dev-only package `replicant_dev_auth`
  (`X-Replicant-Dev-User: <login>`, principals `derived("usr","replicant-dev-user",login)`; grace = platform_admin +
  operator + release roles + approver on every tenant). The evaluator keeps the identity-service
  `POST /internal/v1/authorize` request/decision shape in-process. Live repository-permission checks (identity.md §6.1)
  are not made; tenant role bindings are static. `PUT /v1/admin/role-bindings` answers 501. Prod OIDC/BFF/device-grant
  auth is not implemented (a prod image authenticates only internal-caller tokens, which are themselves local-only).
- **CONTROL-D2 dev:seed scope widened** beyond identity.md L-5 to platform caps, migration status, release assignment
  read/bootstrap and registry publish/build, because M1 has no other admin principal.
- **CONTROL-D3 Bundle promotion without evaluation.** No eval-worker in M1: the local seeder's bundles go
  built -> evaluating -> eval_passed with reason "M1 … eval skipped" (recorded in `bundle_promotion_events`), so
  `assignment:bootstrap` can run. Only in `local`, only for `svc_dev-seeder`.
- **CONTROL-D4 Registry simplifications.** publish: envelope validation only (no kind schemas, no graph compile, global
  scope only); build: `mode: full` only, closure = agents/environments referenced by the selected graphs/policies,
  unpublished closure members skipped with a warning, `prompts: []` (no Langfuse), `cli_images` only for digest-pinned
  images, `node_catalog` placeholder when not published. The dev release-bundles path overwrites its blob/pointer
  (repair of the pre-CONTROL-06 layout) instead of write-once.
- **CONTROL-D5 Intake checks deferred:** quota/budget checks (cost views) and the S3 kill-switch mirror are not read
  (Postgres `tenancy.kill_switches` is); gateway budget-account registration for eval jobs is not done; no Langfuse
  attributes.
- **CONTROL-D6 Audit:** when `audit.emit` rejects an event it runs in a SAVEPOINT, is logged with `log_type=audit`, and
  the business change proceeds (spec: fail the change). Platform-scope events use tenant `replicant-system`.
- **CONTROL-D7 Gitea channel on JobRequest:** `source.channel = github` with `source.github.app_registration = dev` and the
  synthetic installation id (C-JOBREQ has no `gitea` channel); `channel_identity.kind = gitea`.
- **CONTROL-D8 github-integration:** one process runs ingress and consumer; only labelled-issue triggers and PR
  closed/merged `pr.event` are routed (commands, reviews, comments are ignored in M1); admin routes authenticate with the
  dev seed token; no reconciler (Gitea has no deliveries API); `/github/webhook` answers 503.
- **CONTROL-D9 github-token-broker:** Gitea backend only; caller auth by static tokens (`REPLICANT_BROKER_CALLER_TOKENS_FILE`);
  installation id not checked against `github_app.installations`; GitHub App routes 501.
- **CONTROL-D10 run-grants:** `REPLICANT_RUN_GRANT_JWKS_SOURCE=file` only; revocation entries kept 48 h.
- **CONTROL-D11 SQLAlchemy 2.1.6** (the spec's CP-2 version; the brief said 2.0), psycopg 3 instead of asyncpg (the spec
  compose DSN is `postgresql+psycopg://`).
- **WORKER-D2a (correction to D2).** The registry reader follows C-DEF exactly through `replicant_cdef` (CONTROL-06);
  the earlier "whole document" digest alternative was removed (WORKER-05).

## RUNNER (runner-local, repo-mirror, replicant-entrypoint, replicant-agentd, sandbox-sidecars, replicant-qa, CLI/env images)

- **RUNNER-D1 Sandbox network: `replicant-core`, no sbx-edge/sbx-dns/egress-proxy (M1, PLAN.md).** Sandboxes join
  `RUNNER_SANDBOX_NETWORK` (v0: `replicant-core`) and reach `gateway-shim:8420`, `runner-local:8490`, `repo-mirror:8080` and
  `s3.replicant.localhost:8333` directly; bootstrap.json carries those endpoints (`RUNNER_SANDBOX_*_URL`).
  `RUNNER_SANDBOX_DIRECT_EGRESS=true` drops runner-local.yaml's proxy/mirror variables (Squid, Athens, Verdaccio absent),
  so `go mod download` uses proxy.golang.org directly. No egress allowlist is enforced (SEC-EGRESS-* not applicable).
- **RUNNER-D2 runner-local runs as a container on the podman machine**, talking to the VM-side rootless socket
  (`/run/user/501/podman/podman.sock`) through the Docker-compatible API, with `label=disable` on runner-local only.
  Per-run token directories live in the `runner-local-state` volume and are mounted into replicant-agentd with a volume
  `subpath` (read-only); bootstrap.json likewise (`runs/<run_id>/config` → `/etc/replicant`).
- **RUNNER-D3 Pod group details.** (a) A local-only `ar-<body>-volume-init` helper (root, CAP_CHOWN/FOWNER) sets the
  modes kubelet's `fsGroup: 10002` would; volumes are created with `o=uid=10001,gid=10002` because rootless Podman
  otherwise chowns an unowned volume to its first mounter. (b) `ipc`, `uploader-ipc`, `agentd-state` are tmpfs-backed
  named volumes (Docker/Podman tmpfs mounts cannot be shared between containers); `prompt` and `/tmp` are per-container
  tmpfs. (c) All containers of one run share one SELinux MCS level (`label=level:s0:cA,cB` derived from run_id) and the
  tmpfs volumes are mounted with that `context=` (SELinux otherwise denies creating the IPC sockets) — the analogue of a
  pod's shared SELinux context. (d) Native-sidecar semantics are emulated: agentd and watchdog have
  `RestartPolicy on-failure`; they and the pause container are stopped after the uploader exits. (e) No `svc-*`,
  `stub-*`, `builder` (v0), no `podinfo` downward-API volume (the entrypoint falls back to the heartbeat for the cancel
  reason), no shipper. (f) The agent's termination message is written to `/replicant/out/.termination/agent`
  (`REPLICANT_TERMINATION_MESSAGE_PATH`, read back from the stopped container) because `/dev/termination-log` does not
  survive a stopped Podman container (local-runner.md X-21 "docker cp"). (g) Resource limits are clamped to
  `RUNNER_MAX_SANDBOX_CPUS` (4) / `RUNNER_MAX_SANDBOX_MEMORY` (4Gi) on the 8 GiB VM.
- **RUNNER-D4 Isolation reporting.** Runtime is runc; `RunStatus.isolation` reports the RuntimeClass mapped from the hint
  (`kata-clh`) as runner-api would, while the pod label `replicant.example.com/isolation` carries the runtime that actually
  ran (`runc`). Parity P-06.
- **RUNNER-D5 Sandbox S3 credentials: mode `static` in M1.** `auto` probes STS `AssumeRole` with the session policy; the
  v0 SeaweedFS has no STS, so `GET /sandbox/v1/credentials/aws` vends the configured `replicant-services` keys (not
  prefix-scoped). RC-SBX-04 / RC-ART-06 are therefore not satisfied locally. runner-local itself also uses
  `replicant-services` (`RUNNER_S3_CREDENTIALS_FILE`), because the spec's `runner-local` identity lacks read/list on the
  artifacts and transcripts buckets that A-4, the inbox and the evidence check need (RUNNER-05).
- **RUNNER-D6 RC-ADM-05 status.** A wrong `callback.namespace` returns `422 admission_denied` (test-strategy RC-ADM-05)
  instead of runner-service.md A-2's `400 invalid_request`; other A-2 checks stay 400.
- **RUNNER-D7 Notifier payload: plain JSON.** Per WORKER-01 the `run.status_changed` Signal uses the default converter
  (`json/plain`); `RUNNER_TEMPORAL_PAYLOAD_CODEC=local-v1` enables the encode-only tenant codec (P2). No mTLS (P-03).
- **RUNNER-D8 Admission leniency.** Without `runtime_selection` / `network_profiles` policies in the bundle (the v0
  bundle has none) runner-local applies local defaults (runc; any profile name) — A-9/A-10 are not enforced, RC-ADM-04 is
  not applicable. Tenant concurrency is `RUNNER_MAX_CONCURRENT_RUNS` across the runner (X-23), not a namespace label.
- **RUNNER-D9 Run plan image references by tag.** `run-plan.schema.json` requires `cliImage.image` by digest; local
  images are `localhost/replicant/...:dev` (parity P-31), so prepare (and the smoke driver) ignore exactly that violation
  when `env=local`. Everything else in the plan is validated strictly (exit 40).
- **RUNNER-D10 Entrypoint features not in v0:** SR-1 session restore (a `restore_session` input is ignored → fresh session)
  and session capture (`cli_session_state` not produced); WS-08 pattern redaction (the uploader applies only agentd's
  known-credential redaction, ruleset `redact-local-0`); OTel spans/metrics (telemetry env only when an OTLP endpoint is
  configured); `materialize` prints argv + managed settings only; `contract-test` and `agent-task` subcommands absent.
  A result event without `structured_output` whose `result` text is a JSON object valid against `agent_report` is accepted
  (fallback). MCP servers are rendered only when bootstrap carries their endpoint (M1: none, so `managed-mcp.json` is empty).
- **RUNNER-D11 replicant-qa is the minimal v0 harness:** stages `install` (`go mod download`), `unit` (`go test -json`,
  JUnit rendered), `build` (`go build ./...`) for Go repos, else `not_applicable`; no flake classification, base pass,
  services, image build or agent tasks. Its `qa_report` is a summary document, not the full WS-05 qa-report schema.
- **RUNNER-D12 repo-mirror** proxies to upstream on demand (bare repo per `repo_id`, refreshed when older than 15 s or a
  wanted SHA is missing) instead of a sharded StatefulSet with background refresh; the upstream `owner/name` comes from
  agentd headers (`X-Replicant-Repo-*`, from bootstrap.json) and is checked against the Gitea API id before serving.
  Only the primary repo is mapped (additional repos are not in M1).
- **RUNNER-D13 Not implemented in M1:** lease expiry → `NodeLost` works only from heartbeats (no node concept);
  RC-CAN-01 (cancel while Pending with a paused runner) has no test hook; CEL validation of records is not emulated;
  `GET /v1/runs` pagination tokens are HMAC'd per process (invalid after a restart); kind/k8s profile (§3) not built.
- **PLATFORM-D14 (integration) Gitea by service name for git clients.** repo-mirror upstream and publisher-worker's Gitea
  base URL use `http://gitea:3300` instead of `git.replicant.localhost:3300` (libcurl hard-wires `*.localhost` to loopback).
  Repo identity in JobRequests, RunSpecs and `ReplicantRepos` stays `git.replicant.localhost:3300/<owner>/<name>`.
- **PLATFORM-D15 (M2) Grafana without SSO, Langfuse deferred.** No dex in v0: Grafana runs with anonymous Viewer access
  (admin login form enabled) instead of Dex OAuth. Langfuse (web, worker, ClickHouse, Valkey, Postgres; ~2.8 GB by the
  spec estimate) is not started in M2 to stay inside the 8 GiB VM; Alloy's Langfuse exporter fails and retries (log
  noise only). Python `service.name` defaults to the process name (`OTEL_SERVICE_NAME` overrides).
- **PLATFORM-D16 (M2) Go OTel is a minimal OTLP/HTTP JSON exporter** in runner-local and the gateway shim (no OTel SDK),
  spans `runner.agentrun` and `gateway <protocol>` only; replicant-agentd / replicant-entrypoint emit no spans yet, so the
  `sandbox.run` span (T-5b) that shim spans parent under is implied, not exported.
- **PLATFORM-D17 (M3) sbx network on Podman.** `replicant-sbx` gets `x-podman.disable_dns: true` and an explicit gateway
  (172.31.250.1; the gateway is not reachable on any port from sandboxes, checked); sbx-edge's resolver comes from
  the container's resolv.conf at start instead of Docker's 127.0.0.11.
- **PLATFORM-D18 (M3) No package mirrors in v0.** Verdaccio/Athens/proxpi stay in `core` only; sandboxes fetch Go
  modules from proxy.golang.org (+ sum.golang.org) through the egress proxy, allowlisted for sandboxes. SEC-EGRESS-06
  is therefore checked as "go get via the allowlisted path succeeds, a non-allowlisted registry is refused (403)";
  the npm/pip variants need the core profile's mirrors.
- **PLATFORM-D19 (M3) Runner contract notes.** RC-RUN-19 expects Failed/SecurityViolation (C-RUNNER cancel reason
  `security`) where test-strategy says Cancelled; its revocation check uses the exec revocation feed (driver runs are
  not ReplicantJob runs, so they never reach the projection-backed `/grants/revoked`). RC-RUN-22: setup steps run in
  prepare, so the run fails from Provisioning (not via Finalizing) and EnvironmentSetupFailed is retryable per the
  reason table. RC-RUN-03 holds Temporal down 90 s (spec 60 s) so the 60 s minimum deadline falls inside the outage.
  RC-RUN-20's trigger is local (decoy credentials reported by sbx-edge); WS-02 names it only as an example.

## WEB (control-plane-web, plus the control-plane-api endpoints it needed)

- **WEB-01 control-plane-web in the `v0` profile.** The spec puts it in `core`; `compose.v0.yaml` adds `v0` to its
  profiles. Image, port 8400, `/healthz`, `NGINX_PORT`, `REPLICANT_API_UPSTREAM` are the spec's. nginx resolves the
  upstream per request (resolver from the container's resolv.conf) so a recreated control-plane-api does not break the
  proxy. No BFF/OIDC session (ADR-006) in v0: the browser sends `X-Replicant-Dev-User` (static dev users of
  `replicant_dev_auth`), chosen with a local user switcher in the rail.
- **WEB-02 Packet resolution.** `GET /v1/gates/{id}` returns `packet.content` (the approval packet JSON) and
  `packet.resolved` (plan, qa_report, review_report, verification_report, handoff inlined, each ≤ 512 KiB) instead of
  presigned links, because S3 is not reachable from browsers locally. The diff bundle is served same-origin by the new
  `GET /v1/gates/{gate_id}/packet/refs/{ref}` (job:read; only refs listed in the packet, only keys under the gate's
  `tenants/<t>/jobs/<job>/` prefix in the artifacts bucket) and decoded in the browser (fzstd + ustar).
- **WEB-03 Added control-plane-api endpoints (OpenAPI shapes, M1 data sources):** `GET /v1/me` (capabilities from the
  static role table; `memory:admin` = group `replicant-admins`), `GET /v1/tenants`, `GET /v1/jobs/{id}/runs`,
  `GET /v1/jobs/{id}/artifacts`, `POST /v1/jobs/{id}:cancel|:pause|:resume`, `POST /v1/jobs/{id}/steering` (Updates
  through the outbox), `GET /v1/releases/bundles`, `GET /v1/releases/workers` (routing status from the registry: the
  default bundle's build is `current`; pollers are not reported, no DescribeWorkerDeployment), `GET
  /v1/admin/kill-switches` (read only; `PUT` is not implemented, so the UI cannot engage a switch). `/v1/me` adds
  `login`, `groups`, `platform_roles` beyond the `Me` schema for the dev switcher.
- **WEB-04 No SSE.** `/v1/stream` does not exist in M1; the UI polls (jobs, job detail, inbox every 4 s) and the top-bar
  live dot reflects poll health.
- **WEB-05 People directory.** There is no principal lookup API; the UI resolves names only for the six static dev
  users (one `GET /v1/me` per dev user) and shows other principal IDs truncated.
- **WEB-06 Projection lag banner.** M1's `X-Replicant-Projection-Lag-Seconds` is "seconds since the job's last projection
  write", which also grows while a job simply waits at a gate. The UI ignores it on lists and for waiting/paused jobs
  and shows "No update from this job for N min" only for queued/running jobs after 10 min.
- **WEB-07 Rail.** Releases shows only with `release:read` (the API denies it otherwise; the IA shows "Releases (read)"
  to every engineer). Registries, Cost & quotas and Tenant settings are not in the rail (not built).
- **WEB-08 Not built in v0:** registries, cost & quotas, tenant settings, take over, batch approvals, release
  operations (rollback/resume/abort, sign-offs), the failure-rate chart (one bundle, no ramps: nothing to compare),
  transcripts and artifacts tabs (no transcript/artifact projection rows in v0), memory curation (memory-service is
  not deployed: admins get the empty state, others the 403 page), search API (the ⌘K palette filters the jobs and
  gates it has loaded).

## RENAME (orchestrator, 2026-10-25): product rename to Replicant (CCR-ORCH-02)

- **RENAME-D1 Scope.** Every product-named identifier was renamed with a scripted, explicit mapping (dry run with
  per-pattern counts, then apply), across the monorepo and all of `spec/`: 22,061 replacements in 1,170 files and 54
  path renames, plus 6 local secret files (renamed in place, values kept and never printed). Kept: third-party
  identifiers (`default_factory`, `session_factory`, JFrog Artifactory), generic "software factory", "client factory"
  and "app factory", the `prompts/software-factory/` path, and C-IDS ID prefixes. The spec's "do not edit `spec/`" rule
  was lifted for this change by the user's decision.
- **RENAME-D2 Contracts.** Every contract took a major bump (C-ARCH/C-STATE/C-IDS/C-MSG/C-JOBREQ/C-ART/C-PG 2.0.0,
  C-RUNNER/C-CRD/C-DEF 3.0.0). Schema `$id` hosts changed (`schemas.replicant.example.com`) but the `$id` path majors
  (`/v1.json`, `/v2.json`) were left as they were. Test vectors that hash product-named inputs were regenerated: C-IDS
  trace/span/session seeds, the C-RUNNER `ImplementerRunSpec` `spec_digest`, and the C-DEF digest vectors
  (`replicant_cdef/vectors.json`, `go/cmd/runner-local/testdata/cdef-vectors.json`). Historical records (the CCR log,
  `90-reviews/`, this file and INTERFACES-LOG) keep the contract versions they cited; their identifiers were renamed so
  they still match the code.
- **RENAME-D3 No Alembic re-baseline.** Not needed: the migrations apply the owner SQL verbatim and the roles are
  renamed in that SQL, and checksums are compared only against a live `ops.migration_log`, which `dev down -v` wiped.
  Revision IDs are not product-named. `STRICT=1 validate_migrations.sh` passes.
- **RENAME-D4 Regenerated.** `uv.lock` (`uv lock`), `.venv`, `go.work` (renamed module dirs), Go module paths
  `example.com/replicant/...`, `gofmt -w` on 25 files whose struct-tag alignment shifted. `agentrun_types.go` matches
  `crd2go.py` output for C-CRD 3.0.0 after gofmt.
- **RENAME-D5 Unrelated stale test fixed.** `cli/tests/test_cli.py::test_v0_profile_services` expected 20 v0 services;
  M3 added `sbx-dns`, `sbx-edge` and `egress-proxy` (23). This predates the rename.
- **RENAME-D6 Known gap, unchanged by the rename.** The CLI's service loader reads only `compose.yaml`, where
  `control-plane-web` is in profile `core`, so `replicant dev up --profile v0` does not start the UI even though
  `compose.v0.yaml` adds `v0` to it (WEB-01). Start it as `web/control-plane-web/README.md` says.

## NOTIFY (notification fan-out, UI inbox, reminders, Slack DMs; 2026-10-25)

Spec: `spec/07-control-plane-and-ux/notifications.md`, `approvals.md` §1/§4/§6/§8, `identity.md`. Product decision
(user, ADR-009): a job stays in PR review until a human merges or closes the PR; people are notified while it waits.

- **NOTIFY-D1 Outbox columns (migration `202610250020_notify_outbox`, expand-only).** `notify.deliveries` gains
  `payload`, `attempts`, `next_attempt_at`, `last_error` (notify.sql has no retry state, N-1 requires retries);
  new table `slack_app.notification_posts` (slack-integration's per-delivery idempotency record; `message_refs` is
  gate-only); `github_app.comment_refs.purpose` admits `notification` (delivery_id in `delivery_guid`). The STRICT
  catalog diff against the spec DDL therefore reports these three objects as additions.
- **NOTIFY-D2 Scheduler and dispatcher are in-process loops in control-plane-api** (advisory-lock singletons, like the
  action outbox), not a Temporal Schedule. Gate reminders/escalations/expiry stay durable workflow timers
  (approvals.md §1); quiet hours shift *delivery* (`next_attempt_at`) instead of the timer fire time computed at gate
  open. PR-waiting reminders and the lifetime escalation are scheduler sweeps over `jobs_read.job_prs` / `jobs_read.jobs`.
  The workflow's own `repair_limits.pr_wait.reminder_days` reminders (`job.pr_waiting_for_review`) are kept as a UI feed
  item only; the scheduler's daily consolidated reminder replaces their other channels.
- **NOTIFY-D3 Channel defaults beyond the catalogue.** `job.pr_opened` adds `slack_dm` (the requester must act);
  `job.succeeded` adds `originating` (the issue gets the "merged" comment); new keys `job.pr_reminder` (originating,
  ui, slack_dm) and `job.lifetime_escalation` (slack_dm, ui). No digest builder: `digest` is delivered as a Slack DM,
  deferred by quiet hours. `gate.requested` DMs every eligible approver (≤ 5, suggested first) instead of the approval
  channel post + DM only for suggested/high risk; `gate.escalated` DMs the targets instead of `slack_channel` (no
  approval/escalation channels are configured locally). `slack_thread`, `slack_channel`, `jira_comment`: not in v0.
- **NOTIFY-D4 `GET /v1/me/waiting`** (not in control-plane-api.openapi.yaml): `{items[], counts{gates, prs, total},
  as_of}`; gates = the `/v1/approvals?view=mine` set; PRs = open PRs of non-terminal jobs where the caller is the
  requester or a *reviewer* = approved the job's publish gate (v0 requests no reviewers on the Gitea PR). Idle time is
  measured from PR open (no PR activity is projected). `GET/PUT /v1/me/notification-preferences` are the spec's.
- **NOTIFY-D5 Recipients and identity.** No identity-service: eligible approvers / tenant admins come from the static
  dev directory through the same evaluator that authorizes decisions (`gate:decide`, role `tenant_admin`); Slack users
  and Gitea logins are resolved from `identity.links`. Seed step `07-identity-links` writes `identity.users` and
  `identity.links` (gitea + slack, `method dev_seed`) as the DB role `svc_identity_service` via psql inside
  postgres-replicant, instead of identity-service `POST /v1/admin/dev/identity-links`. slack-mock's workspace gains
  `grace` (`U0GRACE01`).
- **NOTIFY-D6 Quiet hours.** Platform default `REPLICANT_NOTIFY_QUIET_HOURS=20:00-08:00,weekends` in the person's
  timezone (`identity.users.timezone`, else `REPLICANT_NOTIFY_DEFAULT_TIMEZONE`). A `quiet_hours` preference replaces it
  entirely (the schema has no weekend flag); `start == end` means none. Escalations are never delayed.
- **NOTIFY-D7 Compose duplicate keys (pre-existing bug, fixed).** `compose.v0.yaml` declared `control-plane-api` and
  `replicant-worker` twice; YAML keeps the last mapping, so their environment overrides (worker
  `REPLICANT_CONTROL_PLANE_TOKEN_FILE`, OTLP endpoints) were silently dropped and every worker notification was
  refused with 401 and dropped. The `depends_on: !reset` now lives in the single block per service.
- **NOTIFY-D8 RENAME-D6 fixed.** `control-plane-web`, `slack-integration` and `slack-mock` list `v0` in
  `compose.yaml` (same convention as the other v0 services), and the CLI's service loader now applies the v0 override
  files (`profiles`, `image`, `build`, `depends_on`), so `replicant dev up --profile v0` starts all three.
- **NOTIFY-D9 Slack scope.** slack-integration sends DMs (`conversations.open` + `chat.postMessage`, Block Kit with URL
  buttons) and verifies signed Slack requests; gate decision buttons/modals, the approval-channel message and thread
  reminders, App Home and the 20 DM/hour summary are not implemented (decide in the UI/API).
- **NOTIFY-D10 Worker.** `notify.job_event` is now also called for `pr_opened` (Publisher node, effect `notify_pr`) and
  for every terminal status (`finalize`, effect `notify_terminal`), both fire-and-forget (`block=False`) behind
  `workflow.patched` markers; the publisher's result carries `diff_stat {files, additions, deletions}` from the
  approved patch.
- **NOTIFY-D11 UI.** New rail item "Waiting on you" (`/waiting`, `G W`, first in the Work group and the ⌘K
  Navigation group) with an accent count badge polled every 4 s (60 s while the endpoint is missing; a 404 hides it).
  ui/README.md has only the Approvals count as an accent element; now both show, also as icon badges below 1200 px
  (app.css hid the collapsed-rail count; one override in web.css). The tab title becomes "(n) replicant".

### NOTIFY phase 2 (Slack decisions, channel threads, digest, App Home; 2026-10-25)

- **NOTIFY-D12 Slack gate decisions.** Gate DMs and the channel gate message carry Approve (with a confirm dialog) and
  Reject (opens a modal; the comment is required by C-MSG). Request changes, rerun QA and answers stay UI-only;
  `critical` gates get no buttons (A-7). slack-integration verifies the v0 signature, dedupes on
  `interaction_dedupe`, resolves the clicker through `identity.links` (unlinked: ephemeral link prompt), writes
  `pending_decisions` and calls `POST /internal/v1/gates/{id}/decisions` (Idempotency-Key = decision_id; 202 is retried
  every 5 s by an in-process retrier). That route now applies the same `gate:decide` check as the UI route (it was
  missing) before the existing pre-checks (separation of duties, digest, channel, state). Outcomes re-render the clicked
  message in place (`resolved_blocks`: decision buttons removed, URL buttons kept, an outcome context line);
  already-decided/closed gates show "Already approved in the UI" and the clicker gets an ephemeral "Not recorded";
  stale digests and refusals are ephemeral only. Every gate decision, from any channel, re-renders all of the gate's
  Slack messages through `notify.gate_resolved` -> slack-integration `POST /internal/v1/gates/{id}/resolved`
  (a `notify.deliveries` row with `payload.op`, so it is retried). The local publish gate admits `slack`
  (`local_bundle` channels ui/cli/slack). Acks use an empty 200 (not `response_url`); Socket Mode is not implemented.
- **NOTIFY-D13 Channel threads.** The tenant's `approval_targets.slack.approval_channel` (seed step 08: payments ->
  `C0PAYAPPR1`) gets one parent message per job on its first event; gate requests (with buttons), decisions, PR opened,
  PR reminders and terminal notices reply in that thread (delivery channel `slack_thread`, never deferred by quiet
  hours). The parent ts is stored in the new `slack_app.job_threads` (migration `202610250021_slack_threads`, which also
  adds `slack_app.notification_posts.blocks` so a message can be re-rendered). Per-repo channels: not in v0 (the
  repo-settings schema has no Slack field); tenant-level only. `notifications_channel` and escalation targets unused.
- **NOTIFY-D14 Daily digest.** The scheduler sends one "Replicant digest" DM per person at each of their digest times
  (preferences `digest.times`, default `REPLICANT_NOTIFY_DIGEST_TIMES`=09:30; `weekdays_only` default true; their
  timezone; skipped in quiet hours, sent by a later sweep within `digest_grace_seconds`=6 h; once per slot) listing
  what `/waiting` lists. People with more than `REPLICANT_NOTIFY_DIGEST_THRESHOLD` (5) waiting items get no per-item
  reminder DMs (gate and PR reminders keep their UI feed item and issue comment); the digest carries them. `digest`
  channel rows of other events are still sent as DMs (NOTIFY-D3), not batched into the digest.
- **NOTIFY-D15 App Home.** `app_home_opened` (and every DM slack-integration sends to a person) publishes the Home tab
  (`views.publish`) with the same list as `/waiting`, fetched from the new `GET /internal/v1/principals/{id}/waiting`
  (`service:slack-integration`; caller token `slack_integration_cp_token`). No refresh on projection changes.
- **NOTIFY-D16 Test hooks instead of env.** The short PR idle threshold is no longer written to `dev/compose/.env`
  (`--pr-reminder-after` removed): `PUT /v1/admin/dev/notify-overrides` (replicant_dev_auth, dev seed token, in memory,
  `{}` restores the env values) overrides `pr_idle_seconds`, `pr_remind_every_seconds`, `digest_threshold`, ... for
  the E2Es, which re-assert it while waiting (a recreated control-plane-api drops it). Re-seeding a bundle after the
  worker's local definitions change: `POST /v1/admin/dev/release-bundles {make_default: true}` moves the local default
  (dev-only stand-in for promote + set_default; seed step 05 sends it), so no `dev down -v` is needed.

## GRAPH (job graph view and agent-activity drill-down, 2026-10-25)

User request: "a view of the graph in the UI … a detailed view of what the agents are doing". Job detail gains a
**Graph** tab (`/jobs/:id/graph?node=<key>&run=<run_id>`); the Runs tab and the live-run strip link into it.

- **GRAPH-D1 Status source.** Per-node status is derived from what the workflow already records, not a new tracker:
  `node.started` / `node.completed` / `node.failed` rows of `jobs_read.job_timeline` (visits, outcome, durations,
  taken edges = consecutive node entries), the JobSnapshot (status, `current_node`, gates) and `jobs_read.job_runs`.
  No `job.snapshot` Query. A skipped node (guard false) and a not-required gate show as `skipped`; an open node of a
  terminal job shows `failed` (job failed) or `cancelled`; an open `human_gate` with an open gate of its kind, an open
  `pr_feedback_wait` without a live run, or a paused job's open node shows `waiting`. Timeline events carry no cycle,
  so visits across repair cycles are counted per node, not per cycle.
- **GRAPH-D2 Graph definition.** The graph is the job's pinned Release Bundle's `graphs[workflow_type]` definition
  (registry row, else the S3 pointer), read as the digest-verified blob with replicant_cdef. It is the base definition,
  not the worker's composed graph (identical in M1: `graph.compose` applies no modifiers). Bundles that fell back to the
  worker's built-in local set (no registry blob) get `graph: null, unavailable_reason: definition_not_resolvable`.
- **GRAPH-D3 Layout.** A small in-repo layered layout (`src/lib/graph.ts`: DFS back edges = loops routed in lanes on
  the right, longest-path ranks, barycenter ordering), rendered as inline SVG; no dagre/elk dependency. `end:*`
  terminals are not drawn except the one the job reached; a node's exits (including ends) are listed in its panel.
- **GRAPH-D4 Activity source.** The feed reads the run's spool chunks from the transcripts bucket
  (`<run prefix>{cli_events,transcript,sandbox_log}/part-NNNNN.ndjson.zst`): Claude Code stream-json (`cli_events`) for
  assistant text, tool calls and results (paired by `tool_use_id`), per-message usage and the final result; the
  `replicant.transcript/v1` records only for what stdout lacks (task prompt, steering), or for everything when
  `cli_events` is absent; `sandbox_log` as plain log lines (QA harness, bootstrap). Not Loki: the spec's live tail
  (`GET /v1/jobs/{job_id}/runs/{run_id}/live`, SSE from Loki) is not built; Loki is only in the observability profile
  and sandbox stdout does not carry the transcript. Edit/MultiEdit/Write diffs are rebuilt from the tool input
  (`difflib`, 2 lines of context). Truncation: assistant text 8 KB, tool input 4 KB, tool result 4 KB (head+tail),
  diff 16 KB, 400 log lines, 3000 items (tail).
- **GRAPH-D5 Live chunks (uploader change).** The uploader shipped only closed spool parts (rotated every 30 s), so a
  20-second scripted run had nothing to show until it ended. sandbox-sidecars' uploader now also writes a provisional
  copy of each type's open part every 3 s when it grew (`<type>/live/part-NNNNN.ndjson.zst`, whole lines only,
  agentd known-credential redaction, same tags), not listed in the run manifest; readers use a `live/` part only
  while no final part of the same name exists. The `live/` objects stay after the run (same retention tag as the
  transcript); not a C-ART type.
- **GRAPH-D6 Polling, not SSE.** Like WEB-04: the activity polls every 3 s while the run is Pending/Provisioning/
  Running/Finalizing, the graph every 4 s while the job is open. API reads decode cached chunks (LRU by key+ETag).
- **GRAPH-D7 Usage.** Token/cost rows come from the gateway shim's durable usage log (`REPLICANT_GATEWAY_USAGE_S3_URI`,
  default `s3://<artifacts>/platform/gateway-usage/`), scanning the hour partitions of the run window (≤ 8 h, ≤ 4000
  objects; write-once objects cached). The shim flushes in batches, so while a run is live the UI shows the
  transcript's per-message usage labelled "(transcript)" and cost "pending". `caller` (token/grant JTIs) is never
  returned. `cost.job_rollups` is still not populated (usage-ingestor not in v0).
- **GRAPH-D8 Redaction.** Chunks already passed agentd's known-credential redaction (stage 1). The API additionally
  runs every surfaced string through WS-08's source-side redactor (`replicant_telemetry.redact.redact_text`, gitleaks
  replicant rules + PII rules, fail closed) and masks JWTs and `Bearer`/`Authorization:` values. The 220 upstream
  gitleaks default rules (Alloy's second pass) are not applied. The task prompt and steering are shown only inside
  labelled untrusted-quote blocks.
- **GRAPH-D9 Observe links.** Grafana Explore links are built in the browser: Loki datasource `loki-sandbox` with the
  server-built selector `{service=~"replicant-sandbox.*"} | run_id="<run>"` (no user LogQL) and Tempo with the job's
  T-1 trace ID. They resolve only with the observability profile up.
- **GRAPH-D10 control-plane-api dependencies.** `zstandard==0.25.0` and the workspace `replicant-telemetry` (already
  installed in the image, now declared).

## ORCH (orchestration engine, 2026-10-26)

- **ORCH-D1 Interpreter-only graph engine (deviates from D-01 / ADR-01-001).** The ReplicantJob workflow runs every
  graph on `InterpreterEngine` (`graphs/engines/interpreter.py`); LangGraph is no longer a dependency of
  replicant-worker. Rationale: both engines passed the same scenario set, and LangGraph state held only
  `start_at`/`terminal`/`trail` with an in-memory checkpointer. Gates, loops, `maxVisits`, routing (FGL guards),
  Continue-As-New boundaries and node events all live in host code (`workflows/replicant_job.py`), so LangGraph
  added a dependency tree, sandbox passthroughs and a second engine to test without adding behaviour. Removed:
  `replicant_worker/adapters/langgraph/` (PregelEngine), the `GraphEngine` protocol (only chose between engines),
  the `REPLICANT_GRAPH_ENGINE` setting (`build_config.ENGINE` is now the constant `interpreter`, still advertised as
  NodeCatalog `spec.engine`), `tools/graph_codegen.py`, `tests/compare_engines.py` and the scenario runner's
  dual-engine `--dump` mode, the langgraph/langchain_core/langsmith/ormsgpack/xxhash workflow-sandbox passthroughs
  (cli.py, tests/harness.py), and `langgraph==1.2.17` with its 21 transitive packages from uv.lock. The worker image
  shrank from 332 MB to 296 MB. The GraphDefinition (C-DEF) format, FGL, GraphSpec compilation, activity IDs and
  timeline events are unchanged, so bundles, the graph view and existing definitions need no change. Ships as a new
  worker build (pinned versioning); jobs pinned to an older build drain on it.

## FIX (assessment follow-up, 2026-10-27; docs/design/v0-assessment.md §3)

- **FIX-D1 (F1) Idempotency keys are scoped and checked.** Gate decision IDs are
  `derived("dec", channel, gate_id, principal_id, Idempotency-Key)`. `Outbox.submit` returns 409
  `idempotency_key_reused` when a stored `intake.pending_actions` row has a different kind, workflow or actor, instead
  of returning or replaying that action's outcome. Test: `test_db_integration.py::test_gate_decision_outbox`.
- **FIX-D2 (F2) Python logs are redacted at the source.** `replicant_telemetry.RedactingFormatter` wraps each
  service's stdout formatter and redacts the whole line, including extras and tracebacks, failing closed. It is wired
  into control-plane-api, github-integration, github-token-broker, slack-integration, replicant-worker,
  publisher-worker and replicant-projector; slack-mock, a test tool, is not covered. `gitleaks-replicant.toml` gains
  `anthropic-api-key` and `github-pat`, local copies of upstream rules, because Alloy (the upstream pass) runs only
  with the observability profile. Go services still have no source-side log redactor.
- **FIX-D3 (F4)** `tests/run_m3_pass.sh` exports `E2E_REQUIRE_X08`, so `--with-observability` makes X0-8 mandatory.
- **FIX-D4 (F6)** `RUNNER_TEST_HOOKS` defaults to `false`; set `RUNNER_TEST_HOOKS=true` for RC-CAN-01 runs.
- **FIX-D5 Codec test vector after the rename.** Vector 2 (`replicant-system`) had been encrypted with the old AAD
  `factory-system`, so `test_codec_test_vectors` failed. Its `expected.data_b64` was regenerated in both the spec and
  the worker copies. No other bytes changed.

## WEB-UIPREF (Simple job view and per-user UI preferences, 2026-10-27)

- **WEB-UIPREF-D1 New schema `web` for `web.ui_preferences`.** The spec has no per-user UI preference store, and
  `notify.preferences` has a closed schema. Migration `202610270001_ui_preferences` creates the schema `web`, roles
  `r_web_read` / `r_web_write` and the table (shaped like `notify.preferences`: per principal, no tenant RLS). The
  `web` schema is not in C-PG's 17-schema list, `10-roles-and-grants.sql`, `naming.SCHEMAS` or
  `ck_migration_log_schema`, so the revision is logged as `multi`. control-plane-api writes it through the existing
  `svc_cpa_notify` module role (granted `r_web_write`) instead of a new module role; reads go through
  `svc_cpa_read` (granted `r_web_read`). No ORM model, so `--verify-drift` does not cover it.
- **WEB-UIPREF-D2 Simple is the default job view.** `/jobs/:id` shows `JobSimple` unless the user chose Detailed;
  a sub-page link (`/jobs/:id/graph|runs|gates|plan|diff|qa`) always opens the Detailed page it names. The ui/ spec
  describes only the Detailed job page. The Simple view's six steps are a fixed grouping of the bugfix phase track
  (`lib/simple.ts` `SIMPLE_STEPS`); another workflow graph would need its own grouping.
- **WEB-UIPREF-D3 Optimistic concurrency on preferences.** `PUT /v1/me/ui-preferences` honours `If-Match` (or body
  `version`) and answers 412 `version_mismatch` on a stale version; without either it is last-writer-wins.
  `PUT /v1/me/notification-preferences` still ignores `version` (unchanged here).
- **WEB-UIPREF-D4 E2E helper pins the Detailed view.** `e2e/helpers.ts` `as()` now sets the user's job view to
  `detailed` by default (the 01–06 specs were written for the Detailed page); `07-simple-view.spec.ts` passes `null`.

## CONFIG (team-owned configuration under RBAC, no rebuilds, 2026-10-27)

- **CONFIG-D1 Projects are not in the spec.** `tenancy.projects` and `tenancy.project_repos` (tenant -> project ->
  repos, one project per repo) exist for ownership and configuration only; cross-repo contracts come later.
  Migration `202610270010_projects_rbac` (logged as `multi`: tenancy + identity). Job lists stay tenant-granular: a
  principal with a role on one project of a tenant sees the tenant's job list (`tenants_with`), while project-scoped
  checks (`repo_ids`, `project_id`) evaluate project bindings.
- **CONFIG-D2 The policy editor is in the UI, not the config repo.** `ui/admin.md` describes read-only policy screens;
  `release-bundle.md` §9.1 allows a policy editor. Team settings are versioned rows in
  `tenancy.project_config_versions` (actor, reason, rollback = a new version copying an old one), not commits to
  replicant-config. One admin can apply any change: no T1 eval and no two-person rule (ADR-013) for team settings.
  The publish gate cannot be turned off (I-3, schema-enforced).
- **CONFIG-D3 Roles come from DB bindings, written by control-plane-api.** `rbac.RoleResolver` builds a user's roles
  from `identity.role_bindings` (current `role_binding_versions` version) and `identity.memberships`, refreshed every
  5 s; the dev header supplies identity only. identity-service is not in v0, so control-plane-api's tenancy module
  role writes `identity.role_bindings` (new grant + RLS policy) for `PUT /v1/projects/{p}/access`, and seed step
  `09-projects-rbac` writes groups, memberships and binding version 1 as `svc_identity_service` (like step 07).
  Static dev-directory platform roles are kept as a union (no lockout); static tenant roles are used only until a
  binding version exists. New role `project_admin` (engineer + approver rights plus `tenant:ops:edit`,
  `tenant:policy:edit`, `identity:admin` on one project); `tenant_admin` gains `identity:admin`, which is no longer
  platform-only (the Admin page capability still comes from platform roles). Platform-scope bindings of a tenant
  role (grace's approver-everywhere) apply to every tenant. New dev user `frank` (project_admin of `prj_ledger`).
- **CONFIG-D4 Derived bundles for pinned settings.** Gates, risk rules and workflow tuning are compiled at intake
  onto the job's base bundle into a derived Release Bundle (`registry.release_bundles` row with
  `base_release_bundle_id`, `request_name = 'project-config'`, `request_digest = sha256(JCS({base, pinned}))`,
  reused for the same pair; assignment reason gets `+layer`). The derived bundle is taken to `promoted` through the
  normal transitions without an eval (it inherits the base's promotion). `registry.tenant_layers` /
  `derived_bundles` (keyed by tenant layer version) are not used. Graph `params` and `maxVisits` overrides are
  compiled into the derived graph definition by control-plane-api (`max_review_rounds` sets the review node's
  `maxVisits`), so the worker only follows `spec.graphs`; it does not apply params itself. A project whose pinned
  settings equal the defaults keeps the base bundle (so E2E-01/04 run on the default bundle unchanged).
- **CONFIG-D5 Worker behaviour from the bundle.** `risk.classify` reads `risk_rules` (`large_change_files`, default 20;
  `sensitive_paths` -> at least high; `forbidden_paths`, also enforced by `verifier.check`); both activities now
  receive `release_bundle_id` (inputs without it use the former constants). `graph.compose` uses the bundle's
  `spec.graphs` mapping (`GRAPH_FILES` only as default). The worker publishes its NodeCatalog to
  `registry/nodecatalogs/replicant-worker/<build>.json` at start; `config:validate` rejects node types not in it and
  checks node configs against their `configSchema` (falls back to the base graphs' node types when absent). The
  built-in-set fallback for a missing bundle pointer now applies only to this build's base dev bundle (or with no
  build id, unit tests); any other `rb_` fails retryably. The worker image was rebuilt once with the same
  `REPLICANT_WORKER_BUILD_ID` (the changes are additive activity inputs, replay-safe), so the default bundle was not
  re-registered.
- **CONFIG-D6 Live trigger settings.** `PUT .../config` pushes the effective trigger settings of every repo of the
  project to github-integration's existing `PUT /internal/v1/repo-settings`, which now also accepts control-plane-api's
  notify delivery token (`updated_by` = the acting user). github-integration resolves who may trigger through
  `identity.links` (kind gitea, active) instead of `REPLICANT_DEV_LINKED_LOGINS` (setting kept, unused); whether that
  principal may start a job is control-plane-api's `job:create` check with project bindings plus the project's
  `min_trigger_role`. `allowed_workflow_types` (`auto` when allowed, else the first type) and `default_priority` are
  honoured in the job request. Approver groups (`approvals.approver_groups`) narrow gate deciders in
  `routes_gates.py` and notify's approver fan-out; `approvals.slack_channel` overrides the tenant's Slack approval
  channel. Live settings are cached for 3 s (`LiveConfig`).
- **CONFIG-D7 Whole-document writes.** `PUT /v1/projects/{p}/config` replaces the project-level document (sections
  left out fall back to the defaults); the UI always sends the whole draft. Audit events
  `admin.project_config_changed`, `admin.project_access_changed`, `admin.project_created`, `admin.project_changed`
  carry the reason in `details` (the audit `reason` column is an enum).
- **CONFIG-D8 E2E-05 on the scripted gateway.** `.env` `REPLICANT_GATEWAY_MODE` was switched to `script` for the
  E2E runs; the e2e asserts every container's image ID and start time and every `localhost/replicant/*` image ID are
  unchanged across the config steps.
- **CONFIG-D9 (fix 2026-10-27) Lists are scoped by project.** `/v1/jobs`, `/v1/approvals` and "Waiting on you"
  (`/v1/me/waiting`, digest, App Home) now show whole tenants only to platform or tenant roles. A member with only a
  project binding sees the jobs, gates and PRs of that project's repos (`auth.scope_filter`; gates are filtered through
  their job's `repo_ids`). This replaces CONFIG-D8's tenant-wide lists. Test:
  `test_db_integration.py::test_job_lists_scoped_to_project_bindings`.
- **CONFIG-D10 (fix 2026-10-27) Project audit events recorded.** Migration `202610270020_audit_projects` registers
  `admin.project_created`, `admin.project_changed` and `admin.project_config_changed` in `audit.event_types`, running
  as `audit_owner`. Project access changes emit the existing `admin.role_binding_changed`, with bindings flattened to
  `group:role` strings. Before this, all four events failed `audit.emit` and were only logged (CONTROL-D6).
- **CONFIG-D11 (fix 2026-10-27) No lock-out on the first access change.** `rbac.RoleResolver.effective` keeps the
  dev directory's groups until memberships exist, and keeps the static tenant roles until the current binding version
  holds tenant- or platform-scope bindings (seed step 09). Before this, a first project-only access change on a fresh
  install removed every static tenant role, including its author's. Test:
  `test_first_project_binding_keeps_static_roles_and_audits`.
- **SEC-D1 (fix 2026-10-27) audit.events has forced row-level security.** It was the one tenant table with RLS
  enabled but not forced, so its owner `audit_owner` (and any SECURITY DEFINER function it owns) bypassed every
  policy, including for UPDATE and DELETE. Migration `202610270030_audit_events_force` forces RLS on the partitioned
  parent and gives `audit_owner` only INSERT and SELECT policies (what `audit.emit`/`emit_restored` need), so the table
  is append-only even for its owner; retention still drops whole partitions. `tools/check_rls.py` no longer exempts
  it. Test: `tests/test_isolation.py::test_every_tenant_table_has_forced_rls` (every table with `tenant_id`,
  partitions included, has ENABLE and FORCE; the allowlist is empty).
- **SEC-D2 (fix 2026-10-27) Isolation suite; hidden projects answer 404 on writes too.** `tests/test_isolation.py`
  checks RLS behaviour on intake, jobs, gates, deliveries and project configs under a tenant-scoped connection, and
  an API matrix: payments users against an `acme-retail` job, run, gate and project, and a project-A member against
  project B of the same tenant (job reads and sub-resources, gate read/decide, job control and steering, project
  read/patch/config/rollback/access, and the lists). Fixes: project writes (`PATCH /v1/projects/{p}`, `PUT .../config`,
  `PUT .../repos/{r}/config`, `POST .../config:rollback`, `PUT .../access`) answered 403 for a project the caller
  cannot read, which confirmed it exists; they now answer 404, and 403 only for callers who can read the project.
  Read routes requiring `job:read|dev:seed` answer 404 rather than 403 when the caller holds no read anywhere.
  `PUT /v1/tenants/{t}/github/repos/{r}/settings` refuses (404) a repo that belongs to another tenant's project.
  `POST /v1/gates/{g}/decisions` checks read access with the gate's repos, so project-scoped members are no longer
  told 404 for gates they can see.
- **SEC-D3 (fix 2026-10-27) Notification preferences use optimistic concurrency.** `PUT
  /v1/me/notification-preferences` takes the version read as `If-Match` or body `version` (If-Match wins), refuses a
  stale one with 412 `version_mismatch` and `current_version`, and `GET`/`PUT` return an `ETag`, like
  `/v1/me/ui-preferences`. A PUT without a version is still last-writer-wins. Version 1 means "nothing stored" (the
  schema minimum, unchanged); new rows start at 2, and a row written earlier at version 1 is still updatable with
  version 1. The web UI has no notification settings page, so no client change. Test:
  `test_db_integration.py::test_notification_preferences_versioned`.
- **WEB-UIPREF-D5 (2026-10-27) Simple view follows the job's workflow.** The plain steps come from the job's own graph
  (`GET /v1/jobs/{id}/graph`), not a fixed bugfix list. Each node's C-DEF `phase` maps to a step, and steps with no
  node are hidden:
  - qa_only shows Requested and Running the tests;
  - hotfix has no Planning step;
  - pr_repair shows Making the fixes and Updating the pull request.

  An unentered step before a reached one shows as not needed. Milestones are named by node type
  (implement_run, qa_run, …), not node key. The bugfix phase track remains the fallback when the graph is
  unavailable. Headlines and `OUTCOME_WORDS` now use the worker's terminal outcome names (`pr_closed_unmerged`,
  `no_change_needed`, …). Before, a PR closed without merging (status `rejected`) read "an approver rejected the
  change".

## PR review comments and the repair loop (D-15), repo instruction files (plan kind-sprouting-diffie §4, §5)

- **MEM-PR-D1 (2026-10-27) Gitea review events carry no line comments.** Gitea sends a submitted review as event
  `pull_request_comment` / `pull_request_approved` / `pull_request_rejected` with type `pull_request_review_*` and only
  `review.content`; a plain PR comment is event `issue_comment` with type `pull_request_comment` (which the normalizer
  used to read as a review). github-integration now reads the sender's latest review on the PR and its line comments
  through the API (`GET /pulls/{n}/reviews`, `/reviews/{id}/comments`). There is no separate
  `pull_request_review_comment.created` event on Gitea; edits and dismissals are not ingested.
- **MEM-PR-D2 (2026-10-27) Reviewer permission is checked by github-integration, through teams.** identity-service is
  not in v0, and Gitea's `collaborators/{u}/permission` needs repo admin. github-integration (broker caller now allowed
  `metadata: read` -> `read:organization`) resolves `repo_permission` from the repo's teams (`units_map["repo.code"]`)
  and team membership; a direct collaborator counts as `write` (Gitea's default collaborator access). Only linked
  humans with admin/maintain/write are signalled; the workflow's `identity.check_repair_trigger` still trusts the
  sender's filter. Bots (incl. replicant-bot) are ignored before routing; every ignored PR comment is logged
  (`pr_feedback.ignored` with the reason); an unlinked human gets one link-your-account reply per repo per 24 h.
- **MEM-PR-D3 (2026-10-27) `push_update` adds a commit on top of the PR head.** The repair run starts from the
  published change (the latest diff bundle is staged as `apply_diff` for `repair` runs with PR feedback), so its diff
  bundle is cumulative from `base_sha`. The Publisher builds that tree and commits it with the current PR branch head
  as parent (`git commit-tree`), then pushes without force: reviewers see one incremental commit. Takeover branches
  stay out of v0 (WORKER-D10).
- **MEM-PR-D4 (2026-10-27) Repo instruction files are quoted ahead of the task.** `repo_instructions:
  quote_as_context` (replicant-entrypoint `repoinstr.go`): `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`
  as checked out at the base commit (before prior diffs are applied), regular files only, 16 KiB in total (UTF-8 safe
  cut), redacted with secret patterns (the verifier's AKIA / private key / gh*_ / xox* / sk-ant- patterns plus
  `password|secret|api_key|token = value`; agentd's known-credential redaction is served only on the uploader socket,
  never to the agent container, and base-commit files cannot hold the run's own credentials; the uploader redacts the
  transcript again), each in `<untrusted_input source="repo_instructions" path="...">`. The block is **prepended** to the first
  message (not appended), so it sits before the task and is visible in the transcript preview. Not for `qa` runs. The
  CLI's own CLAUDE.md loading stays disabled (ADR-007 intent).
- **MEM-PR-D5 (2026-10-27) Replies are PR-level template comments.** `github.post_reply` (publisher-worker, broker
  token, replicant-bot) posts `ack`, `fix_summary`, `question`, `no_action`, `budget_exhausted` as PR comments built
  from structured data (triage classes, paths/lines, commit link, repair cycle `used of max`, QA verdict); model text
  (triage summaries) is sanitized per pr-replies.md §6. No `eyes` reaction, no per-thread replies, no thread
  resolution, no memory footer yet. Each post is idempotent through a `<!-- replicant:reply <activity key> -->` marker.
- **MEM-PR-D6 (2026-10-27) `/replicant no-memory` and `/replicant remember`.** Honoured on a line of their own inside a
  feedback comment, or in a command-only comment of the same author in the same debounced batch
  (`memory_directive` on the MEM-01 items). A command-only batch never starts a repair cycle (`pr_wait` outcome
  `ignored`); a command posted after the batch was processed is not applied retroactively.
- **MEM-PR-D7 (2026-10-27) `feedback.triage` and scripted worker LLM calls.** One tool-less `worker_llm` call per comment
  (max 20) with a pinned inline prompt (`activities/feedback.py`, sha256 in the triage doc); `actionable_fix` with
  confidence < 0.6 becomes `question`; unparseable output becomes `question`. Graph outcome: `actionable` if any fix,
  else `question`, else `no_action` (optional suggestions get the `no_action` reply). The scripted gateway used to answer
  every tool-less request with "OK."; it now serves service callers from `<default set>/<X-Replicant-Script-Key>.yaml`
  (fallback `worker_llm.yaml`) and picks the first turn whose new `match` regexp hits the request body (stateless).
- **MEM-PR-D8 (2026-10-27) The memory node passes `addressed`.** `memory.extract_candidates` gets a top-level
  `addressed` (true only when this cycle's `push` published), since the triage document is written before the repair
  (MEM-01 items otherwise default to addressed for `actionable_fix`).

## MEM (memory subsystem: memory-service, snapshots and injection, curation, 2026-10-27)

- **MEM-D1 (2026-10-27) memory-service is a minimal subset.** `services/memory-service` (FastAPI, login
  `svc_memory_service`, the only writer of schema `memory`) implements snapshots, the per-run injection, candidates,
  findings, outcomes, the job memory view and the admin curation verbs of `memory-service.openapi.yaml`, with simpler
  paths (`POST /internal/v1/injections`, `/internal/v1/findings`). Not built: governance policy documents, flags,
  correction requests, promote/merge/clear-hold, author restrictions, imports, restore-resync, eval components, the
  two-person rule for tenant/org scope. Callers use static tokens (`memory_internal_callers`: control-plane-api
  `memory_cpa_token`, replicant-worker its control-plane token). There is no identity-service, so control-plane-api
  checks `replicant-admins` on every `/v1/admin/memory/**` request (denials audited as `memory.admin_denied` with the
  audit id in the 403) and forwards a caller principal signed with HMAC-SHA256 under its bearer token; memory-service
  verifies the signature and re-checks the group itself (D-25). Admin transactions run with
  `principal_scope = admin:memory`; job paths run in the tenant scope.
- **MEM-D2 (2026-10-27) No embeddings, MCP pull, episodes or holdouts.** Selection is scope-first (tenant, then repo,
  then path globs), ranked by `ts_rank_cd` + specificity + confidence + recency (`U(e)` without the vector and
  outcome terms). Dedupe is the exact content digest, then `pg_trgm` similarity ≥ 0.6 on the same type and scope. A
  0.35–0.6 match goes to one LLM judge call (`memory_dedupe_judge`, purpose `worker_llm` under the candidate's job,
  because the shim binds service callers to a job). Tokens are estimated as chars/4. Role budgets: implementer
  8000/80, repair 6000/60, planner 4000/40, reviewer 3000/30, qa 2000/20. `memory.write_episode` is a no-op. There
  are no holdout arms and no auto-activation: every learned entry waits for an admin, and an admin-authored entry is
  active at once. pgvector and `entry_embeddings` stay for later.
- **MEM-D3 (2026-10-27) Gate comments are a memory source.** A `request_changes` or `reject` decision with a comment
  starts `memory.ingest_gate_feedback` from the gate handler (workflow patch `memory-gate-feedback-v1`; not for
  scripted or evaluation decisions). Extraction runs on the comment, then candidates are written with provenance
  `gate_comment` (author = the approver). Migration `202610270040_memory_v0` adds the source kinds `gate_comment` and
  `review_finding`, and the feedback-ledger prefixes `gate:`, `review:` and `scan:`.
- **MEM-D4 (2026-10-27) Reviewer and QA recurrence.** `memory.record_outcome` (finalize) reads the job's run results:
  reviewer `agent_report.findings` and QA `top_failures`. memory-service records them in the ledger and proposes a
  `negative` candidate (origin `negative_mining`) only when a finding is similar (trigram ≥ 0.6) to one recorded for
  **another job on the same repo**. Outcome metrics come from the same run results and the gate decision records:
  review rounds = reviewer `request_changes` verdicts, plus first-pass QA, first review approved and gate
  request-changes count. `memory.job_outcomes` takes repo, workflow type and bundle from the finalize input or the
  job's snapshot binding.
- **MEM-D5 (2026-10-27) Repo names and source URLs.** The memory schema's repo CHECK has no port, so repos are stored
  as `host/owner/name` with the port stripped (`git.replicant.localhost/acme-payments/ledger-api`). Local Gitea serves
  http, so the same migration lets `provenance.source_url` take `http://` as well.
- **MEM-D6 (2026-10-27) Revocation keeps the snapshot ID.** `memory.create_snapshot(revocation_filter)` returns the
  job's pinned snapshot unchanged. The injection for each run drops entries that are no longer active. They are
  listed under `withheld` in `memory-injection.json`, and `usage_events` records `revoked_midjob`. No derived
  snapshot is written, and nothing is ever added mid-job.
- **MEM-D7 (2026-10-27) The repo scan has no workflow type.** C-JOBREQ, C-DEF and C-MSG freeze the workflow-type
  enum, so the scan works around it:
  - `POST /v1/projects/{p}/repos/{r}:scan` (project admins, or memory admins who can create jobs, since the
    requester must be the caller) submits `workflow_type: docs` with label `replicant-repo-scan`.
  - The router rule `v0-repo-scan` keeps it `docs`, and `graph.compose` (which now receives the job's labels) picks
    graph `repo-scan` (context → scan → memory).
  - That graph and the agent `scanner-claude` ship in the worker's local set: the agent is in the bundle, and the
    graph comes from the bundle when pinned, else from the worker build.
  - `plan_run` now also maps `memory_candidates`, and prompts are looked up by agent name before role.
  - The job ends `no_change_needed`, and its candidates have origin `import` with provenance pointing to the scan
    job.
- **MEM-D8 (2026-10-27) Memory is best effort locally.** If memory-service is unreachable, or a route is missing
  (404, local only), the worker uses the pinned empty snapshot and stages no memory block. A job never fails because
  of memory. Outside local, these errors are retried.
- **MEM-D9 (2026-10-27) Purge propagation, v0.**
  - Before any delete, a deletion-ledger record is written to the audit bucket. Then `memory.purge_entry` erases the
    content and keeps the tombstone.
  - Each affected snapshot export is rewritten as `….purged-<purge_id>.json` (row in
    `snapshot_export_replacements`), and the original is deleted.
  - The `memory-injection.json` artifacts of runs that received the entry are deleted.
  - There is no Langfuse or transcript scrub: transcripts of past runs still contain the rendered block.
- **MEM-D10 (2026-10-27) UI.**
  - `/admin/memory` shows candidates, entries, entry detail (provenance, versions, history, jobs that used it),
    conflicts, quarantine and purge with a dry-run blast radius, export, analytics and admin-authored entries.
  - Analytics compare jobs on the same repo with and without each entry. This is observational, because there are
    no holdouts.
  - "Memory used" (job detail, approval view, one line in the Simple view) reads `GET /v1/jobs/{id}/memory`.
  - Project Overview has "Learn this repo's conventions" for project admins.
- **MEM-PR-D9 (2026-10-27) Two repair cycles locally.** The local bundle had `repair_cycles.default: 0` (plan.md §1.3),
  so the first review on a PR ended the job `repair_budget_exhausted`. It is now 2 (bounded; `budget_exhausted` reply
  and `end:repair_budget_exhausted` after that). Jobs without PR feedback still run no repair cycle (E2E-01 `cycle 0`).
- **MEM-PR-D10 (2026-10-27) `waiting_external` reaches the projection.** `_set_status` changed the status and search
  attributes but emitted no projection event, so a job waiting on its PR showed `running` in `GET /v1/jobs/{id}` and the
  lists. Entering `waiting_external` now emits timeline event `job.waiting` ("Waiting for review on PR #N"), patched
  `status-waiting-external-v1`; leaving the wait is carried by the next node or terminal event. E2E-07 asserts it.
