# Contract change requests — requests to change a frozen Phase 1 contract (ID, requester WS, change, rationale, status). Append-only.

## WS-11 — 2026-10-24T13:51Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-11-01 | WS-11 | C-JOBREQ v1.0.0 (`$defs.requester.properties.channel_identity.properties.kind`), C-MSG v1.0.0 (`$defs.actor.properties.channel_identity.properties.kind`) | Add `gitea` to both `kind` enums (additive). | `repo.provider` already allows `gitea` (local profile) and `PrEventSignal.delivery_id` mentions Gitea, but a Gitea user cannot be represented as a channel identity, so a Gitea-triggered JobRequest or a Gitea-review gate decision cannot carry its true channel identity. Using `github` would collide numeric user IDs between Gitea and GitHub when both run locally (walkthrough 11). Interim fallback if rejected: `kind: github` with `id: "<gitea host>:<numeric id>"` (GitHub IDs are purely numeric, so no collision). See spec/11-local-dev/adr/002-gitea-local-git-server.md | Open |
| CCR-11-02 | WS-11 | C-ARCH v1.0.0 §1.4 and §3 | Replace the local component name `minio` with "S3-compatible server (`s3`, SeaweedFS by default; MinIO from source as profile `s3-minio`)". Contingent on Challenge D-08 (spec/11-local-dev/adr/004-challenge-d08-minio-locally.md). | MinIO's repository is archived (2026-04-25) and the community edition is source-only with no published images; the local stack should not default to an unmaintained server. | Open (depends on ADR-11-004 adjudication) |
| CCR-11-03 | WS-11 | C-ARCH v1.0.0 §3 diagram | Move `langfuse-web`, `langfuse-worker`, `clickhouse`, `valkey`, `postgres-langfuse` from the `observability` profile into `core`. | Bundle builds and the context assembler resolve prompts from Langfuse (C-DEF §4.3, §6); without Langfuse in core no job can run (spec/11-local-dev/adr/005-langfuse-in-core-profile.md). Diagram-only change; no interface change. | Open |

## Orchestrator adjudication — 2026-10-24

| ID | Decision | Contract version |
|---|---|---|
| CCR-11-01 | **Accepted.** `gitea` added to `channel_identity.kind` in JobRequest and workflow-messages actor | C-JOBREQ 1.1.0, C-MSG 1.1.0 |
| CCR-11-02 | **Accepted** with ADR-11-004 (Challenge: D-08, local only). Verified: MinIO repo archived 2026-04-25, no community images. Local object store is `s3` (SeaweedFS default; `s3-minio` profile from source). Production S3 unchanged. D-08 amended | C-ARCH 1.0.1 |
| CCR-11-03 | **Accepted.** Langfuse and its dependencies move to the `core` compose profile | C-ARCH 1.0.1 |

## WS-09 — 2026-10-24

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-09-01 | WS-09 | C-RUNNER (`RunTokenClaims.aud`, `RunTokenResponse`, `/sandbox/v1/token:refresh`), C-IDS §7 | `aud` becomes a single string; the Runner mints one token per audience listed in the run grant; refresh returns `{"tokens": {"<audience>": {"token", "expires_at"}}}`; add env `REPLICANT_RUN_TOKEN_DIR` (files `/run/replicant/tokens/<audience>`), keep `REPLICANT_RUN_TOKEN_FILE` (→ `runner-sandbox` token) during the N/N+1 window; verifiers require `aud` == own name | One multi-audience bearer token is presented to the gateway shim, memory-mcp, context-mcp, and repo-mirror; compromise of any one verifier (or its request logs) allows replay to the others for ≤ 60 min (threat-model T-CRED-02). Spec: spec/09-security/adr/006-per-audience-run-tokens.md | Proposed |
| CCR-09-02 | WS-09 | C-RUNNER (`RunSpec.run_grant`, `RunTokenClaims.grant`, `grant_jti`), C-CRD (`spec.runGrant`, immutable), C-IDS §2 (new ID prefix `rgr_` for grant `jti`) | The Runner-call activity (core) signs a run grant (JWS, KMS key class `run-grant`, ECC P-256) binding tenant, job, run_id, repo_ids, audiences, scopes, budget, `acting_for`, and the RunSpec digest; runner-api verifies it at admission and stores it; every run token embeds it; verifiers check both signatures and that token claims ⊆ grant; core publishes the grant JWKS and a revocation list | Today any party holding the Runner's KMS signing permission can mint tokens for every tenant's repos, memory, and budgets (threat-model §7.3). With grants, a compromised exec side can mint only for in-flight runs within their scope. Schema in spec/09-security/adr/005-run-grants-bound-exec-side-minting.md | Proposed |
| CCR-09-03 | WS-09 | C-RUNNER (`RunTokenClaims`) | Add required claim `acting_for` (principal_id of the requester, or `on_behalf_of` for service-triggered jobs) | context-mcp and memory-mcp must filter repository content by what the requester can read (D-18, no confused deputy); WS-04 already designs requester filtering, which today needs a lookup from `job_id`. An explicit, signed claim removes the lookup and makes the check verifiable | Proposed |
| CCR-09-04 | WS-09 | C-CRD §4 (failure reasons), C-RUNNER `FailureReason`, `CancelRequest.reason` | Add failure reason `SecurityViolation` (non-retryable) and cancel reason `security` | Runs terminated by security automation (Tetragon P1 on the node, token misuse, policy violation) need a distinct, non-retryable outcome so workflows do not retry them and operators see the cause; today they would surface as `CancelledByRequest`/`ControllerError` (retryable) | Proposed |
| CCR-09-05 | WS-09 | C-ART §4 (artifact type registry) | Add type `secret_scan_report`: bucket class `artifacts`, `application/json`, 1 MiB, `r2y`, redaction `n/a` (holds rule IDs, paths, lines, and HMAC fingerprints only, never secret values); producers: verifier step, github-integration pre-post scan | Publish-blocking secret findings need a durable, displayable record (data-handling.md §4) | Proposed |
| CCR-09-06 | WS-09 (with WS-07's CCR-07-01) | ADR-011, C-ARCH §6 | Amend the broker caller allowlist: add `identity-service` (`metadata:read`, `members:read`), `zoekt-indexer` and `ecosystem-indexer` (`contents:read`, `metadata:read`, opted-in repos only); add `checks:write` for github-integration to C-ARCH §6 (ADR-011 already lists it); state that no caller may request `workflows`, `administration`, `secrets`, `actions` and the App registration does not hold them | C-STATE says identity-service checks live repo permission with a broker token and that the Zoekt index is derived from GitHub, but neither is an allowlisted caller (control-review G-06, G-15) | Proposed |
| CCR-09-07 | WS-09 | C-ARCH §4 (production composition), ADR-007 | The exec cluster lives in its own AWS account (`replicant-<env>-exec`); cross-account paths are PrivateLink endpoint services plus cross-account S3 bucket and KMS key policies naming two exec roles; sandbox subnets have no default route | Confirms ADR-007 and closes the shared-account blast radius after a sandbox escape (spec/09-security/adr/001-confirm-adr-007-exec-cluster-separate-account.md). WS-10's bucket policies already assume separate core and exec account IDs | Proposed |

## Orchestrator adjudication — 2026-10-24 (WS-09 requests)

| ID | Decision | Notes |
|---|---|---|
| CCR-09-01 | **Accepted** | Per-audience run tokens. Applied by WS-00 (contract owner) |
| CCR-09-02 | **Accepted** | Core-signed run grants in RunSpec, CRD, token claims; `rgr_` prefix |
| CCR-09-03 | **Accepted** | Required `acting_for` claim |
| CCR-09-04 | **Accepted** | `SecurityViolation` failure reason; `security` cancel reason |
| CCR-09-05 | **Accepted** | `secret_scan_report` artifact type |
| CCR-09-06 | **Accepted** | Broker caller allowlist amendment; never-granted permissions list |
| CCR-09-07 | **Accepted** | Exec cluster in its own AWS account; strengthens ADR-007 |
| Residual risk R-04 | **Accepted for v1 with detection** (orchestrator) | Compromised runner-api can vend S3 sessions for any tenant's in-flight run prefixes; flagged to the user in the final report |

## WS-06 — 2026-10-24T14:00Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-06-01 | WS-06 | C-JOBREQ (`$defs.options`), C-MSG (`JobSnapshot`) | (a) Add optional `options.evaluation` = `EvaluationOptions` from `spec/06-releases-and-evals/schemas/evaluation-options.schema.json` (`mode: eval\|shadow`, `gate_script_uri`, `memory_snapshot_id`, `memory_scope_context`, `shadow_of_job_id`, `publish_to_eval_git`, `case_deadline_seconds`). Accepted only from principals holding `release:pin` and `eval:run`; otherwise 403. Implies `dry_run` semantics (no publish) unless `publish_to_eval_git` and mode `eval`. (b) Add optional `JobSnapshot.evaluation = {mode, eval_run_id?, shadow_of_job_id?}` so projections and UIs can filter eval and shadow jobs. Timeline event `gate.auto_resolved` uses the existing `gate.` prefix (no change) | Eval and shadow jobs must open real gates without a human deciding them (C-MSG V8 forbids service deciders) and must never publish. Resolving gates inside the workflow from a script keeps V8 and D-15 untouched for all other jobs (ADR-06-006) | Proposed |
| CCR-06-02 | WS-06 | C-DEF §6 (context assembler row), C-ART §4 (`registry_blob` description) | Bundle builds write every pinned prompt's canonical body `JCS({type, prompt, config})` to `registry` bucket `blobs/sha256/<prompt digest hex>.json` ("prompt escrow"). The context assembler and in-worker prompt loaders read Langfuse by `(name, version)` as today and, **on 404 only**, read the escrow blob, verifying the same digest. Digest mismatch from Langfuse still fails closed | Langfuse fires `deleted` webhooks for removed prompt versions (versions can be deleted) [verified: https://langfuse.com/docs/prompt-management/features/webhooks-slack-integrations, 2026-10-24]; protected labels need the Enterprise license. A 30-day pinned job must not depend on nobody deleting a version (D-03 reproducibility) | Proposed |
| CCR-06-03 | WS-06 | C-IDS §2 (ID catalog), §4 (workflow types), C-MSG §2 (messages) | (a) New ID prefixes: `rpl_<ulid>` (release pipeline = ReleasePipeline workflow ID), `cmp_<ulid>` (eval comparison), `prop_<ulid>` (improvement proposal), `lbl_<ulid>` (human eval label). (b) New workflow types on `eval-worker` / `replicant-eval`: `ReleasePipeline`, `EvalComparisonRun` (`<rpl_id>:cmp:<n>`), and system workflows `sys:sentinel-eval` (`SentinelEval`), `sys:judge-drift-check` (`JudgeDriftCheck`), `sys:golden-miner` (`GoldenMiner`), `sys:golden-harvester` (`GoldenHarvester`), `sys:improvement-proposer` (`ImprovementProposer`), `sys:cli-upgrade-watcher` (`CliUpgradeWatcher`), `sys:online-metrics` (`OnlineMetrics`). (c) New messages on `ReleasePipeline`: Updates `release.signoff` (Update ID = `decision_id`; validators: actor `usr_`, pending sign-off point, `presented_digest` matches evidence), `release.abort` and `release.hold` (Update ID = `action_id`); Query `release.snapshot`. Sender: control-plane-api only. (d) Service principals `svc_release-controller`, `svc_improvement-proposer` in the identity seed | Promotion is orchestrated durably with human sign-offs as validated Updates, mirroring ADR-001 (promotion.md §3) | Proposed |
| CCR-06-04 | WS-06 | C-IDS §11 (Prometheus label keys) | Allow labels `release_cohort` (`default`, `candidate`, `holdback`, `experiment`, `override`, `migrated`, `eval`, `shadow`) and `bundle` (12-character short base bundle ID for bundles with an active assignment, else `other`; at most about 4 live values) | Canary guardrails and release dashboards compare cohorts and bundles; without these labels online metrics cannot be attributed to releases (online-metrics.md §3, cardinality computed there) | Proposed |
| CCR-06-05 | WS-06 | C-DEF §4.4, §4.5 | (a) Clarify assignable states: `default` must be `promoted`; `tenant_overrides` `promoted` or `deprecated` (never `revoked`); `ramp.candidate` must be `canary` (purpose canary) or `promoted`/`deprecated` (purpose holdback); eval tenant any state except `revoked`/`withdrawn`. (b) Optional: replace the single `ramp` with `ramps[]` (≤ 3) each with `bucket_lo`, using `in_ramp = bucket_lo ≤ bucket(job_id) < bucket_lo + percent×100`; ranges must not overlap. With one ramp and `bucket_lo = 0` the formula is today's exactly | (a) §4.4 says only promoted bundles are assignable to non-eval tenants, while §4.5 ramps unpromoted candidates to live traffic. (b) One ramp slot serializes canaries, holdbacks and experiments (promotion.md §12) | Proposed ((a) required, (b) optional) |
| CCR-06-06 | WS-06 | ADR-013 (Decision, tightening check) | Replace the bindings clause "bindings reference only definitions already in B's closure, or tenant definitions that passed their own eval tier" with the per-policy-type safety order of ADR-06-001: binding changes that move a repo to a different environment or agent are behavioral (targeted T1 + two-person sign-off); only `check_only` node insertions and no-op selectors are tightening. List tenant-layer-eligible policy types (`router_rules`, `runner_polling` are global only) | Moving a repo between two definitions that are both in B's closure still runs an unmeasured (codebase type, definition) combination. Decides OQ-00-05 consistently with D-03 | Proposed |

## WS-03 — 2026-10-24T14:00Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-03-01 | WS-03 | C-CRD §4 (failure reasons), C-RUNNER `FailureReason` | Add failure reason `EnvironmentSetupFailed` (phase reached via `Finalizing`, retryable: yes): a bootstrap step with `onFailure: fail` or a sidecar service readiness/seed failed. Entrypoint exit codes 44/45 map to it (spec/03-agents-and-tools/cli-invocation.md §4.6) | Today such failures surface as `AgentExitedNonZero`, which conflates broken environments (mirror outage, lockfile drift, missing service) with agent crashes; right-sizing, alerting, and retry policy need to distinguish them. Fallback until accepted: `AgentExitedNonZero` with message prefix `EnvironmentSetupFailed:` | Proposed |
| CCR-03-02 | WS-03 (with WS-07 for the API) | C-DEF §3 (selectors), §4.2 (publish API) | (a) An unpinned selector `agent/<name>` resolves to the registry **head**: the version whose content matches the document at the most recent publish from the config repo's default branch (table `registry.definition_heads`), not "latest active" by version number. (b) `POST /v1/registry/publish` accepts `"dry_run": true` (full validation and reference resolution, no writes) and requires the request to contain every document of the commit so deletions remove heads | (a) A `git revert` to older content produces an identical digest, so publish returns the existing older version; with max-version semantics the bad newer version would stay "latest" and the revert would not take effect. (b) PR CI must check references against every environment's registry before merge (spec/03-agents-and-tools/config-publishing.md §2 step 5) | Proposed |
| CCR-03-03 | WS-03 | C-ARCH §1.1, §2 invariants, §4; ADR-007 path list | Add components: `replicant-gateway-shim` (core / `replicant-gateway`, Go, WS-03; internal NLB `ai-gateway.<env>.replicant.internal` reachable from sandbox node pools and core workers; sole egress to the enterprise AI gateway) with Valkey `gateway-state`; `replicant-agentd` (sandbox pod native sidecar, WS-03); `profile-rightsizer` (job in usage-ingestor). The C-ARCH row "Internal AI/ML gateway" becomes the upstream of the shim; the ADR-007 path "sandboxes → AI gateway (FQDN policy)" becomes "sandboxes → replicant-gateway-shim NLB" (under CCR-09-07, a PrivateLink endpoint service) | OQ-00-09 gateway design (spec/03-agents-and-tools/gateway.md, ADR-03-005); sandboxes then never reach the enterprise gateway directly, and the enterprise credential lives only in core | Proposed |
| CCR-03-04 | WS-03 | C-IDS §2, §7 | New ID prefixes: `gwu_` (gateway usage event, ULID), `rsp_` (right-sizing proposal, ULID), `pub_` (registry publish request, ULID), `rec_` (cost reconciliation run, ULID). New headers on the AI gateway path: `X-Replicant-Retry-Attempt` (agentd → shim), `X-Replicant-Request-Id` (= `gwu_…`), `X-Replicant-Budget-Run-Limit-Usd`, `X-Replicant-Budget-Run-Spent-Usd`, `X-Replicant-Budget-Job-Spent-Usd`, `X-Replicant-Budget-Exceeded` (`run`/`job`/`tenant_period`), `X-Replicant-Limit` (shim → agentd) | Used by cost DDL, usage events, gateway OpenAPI (spec/03-agents-and-tools/gateway-interface.openapi.yaml) | Proposed |

## Orchestrator adjudication — 2026-10-24 (WS-03 requests)

| ID | Decision | Notes |
|---|---|---|
| CCR-03-01 | **Accepted** | `EnvironmentSetupFailed` failure reason |
| CCR-03-02 | **Accepted** | Unpinned selector → registry head; publish `dry_run` |
| CCR-03-03 | **Accepted** | Components added; shim is Go and never connects to Postgres (consistent with D-23/D-26) |
| CCR-03-04 | **Accepted** | ID prefixes and gateway headers |
| Challenge: D-12 (WS-03 ADR-008) | **Pending** WS-02 host-footprint evidence | Schema already carries `memoryRequestPolicy`; decision is configuration-level |

## Orchestrator adjudication — 2026-10-24 (WS-06 requests)

| ID | Decision | Notes |
|---|---|---|
| CCR-06-01 | **Accepted** | Evaluation options; gates resolved in workflow |
| CCR-06-02 | **Accepted** | Prompt escrow in registry bucket |
| CCR-06-03 | **Accepted** | IDs, workflow types, release Updates/Query, principals |
| CCR-06-04 | **Accepted** | Bounded Prometheus labels |
| CCR-06-05 | **(a) Accepted; (b) accepted conditionally** on V06-14 verification by WS-01; fallback documented |
| CCR-06-06 | **Accepted** | ADR-013 text aligned with WS-06 ADR-001 |
| OQ-00-05 | **Resolved by WS-06 ADR-001**: tightening-only derived bundles auto-promote at tier T0 under four mechanical checks |
| OQ-06-11 | **Accepted** (orchestrator): guardrail rollbacks may act on derived data because the action only reverts to a known-good bundle |

## WS-04 — 2026-10-24T14:06Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-04-01 | WS-04 | C-ART §1 (who may write), §2 (key layout), §2 rule 4 | (a) memory-service may **delete all object versions** of (i) `memory_snapshot_export` objects in the `memory` bucket and (ii) `memory_injection` objects (`tenants/<t>/jobs/<j>/runs/<r>/inputs/memory-injection.json`) in the `artifacts` bucket, only when executing an admin purge (`mprg_…`), each deletion audited. (b) New write-once key in the `memory` bucket for the re-materialized export after a purge: `tenants/<tenant_id>/snapshots/<memory_snapshot_id>.purged-<purge_id>.json` (same type `memory_snapshot_export`, items replaced by tombstones). Rule 4 ("nothing is overwritten") is preserved: deletion plus a new key, never overwrite | D-14 requires memory to be deletable, and the brief requires deletions to propagate to derived snapshots. Both object types hold verbatim memory content for 2 years (r2y), so a purged secret or PII would otherwise survive in S3 for up to 2 years. Design: spec/04-memory-and-context/data-model.md §8 | Proposed |
| CCR-04-02 | WS-04 | C-ART §4 (artifact type registry) | Add type `context_assembly_report`: bucket class `artifacts`, `application/json`, max 1 MiB, retention `r2y`, redaction `n/a` (counts, refs, digests only; no untrusted text), producer context assembler (`context.stage_inputs`) → UI, eval, Langfuse metadata; stored at `runs/<run_id>/inputs/context-assembly-report.json` | D-14 / brief: the Context Assembler must "record what was cut". Schema: spec/04-memory-and-context/schemas/context-assembly-report.schema.json. Fallback if rejected: embed the report in the inputs manifest's listed `task_packet` | Proposed |
| CCR-04-03 | WS-04 | C-IDS §2 (identifier catalog), §12 (regexes) | New ULID-based prefixes: `mcfl_` (memory conflict), `mqtn_` (memory quarantine / bulk disable), `mprg_` (memory purge), `mexp_` (memory export; C-ART already uses `<export_id>` in `memory`-bucket keys without a format), `mact_` (memory curator action). Regex `^(mcfl|mqtn|mprg|mexp|mact)_[0-9a-hjkmnp-tv-z]{26}$`. Generated by memory-service | These IDs appear in the admin API, audit events, S3 keys (`mexp_`, `mprg_`), and the UI; C-IDS requires a CCR for new identifier types. DDL: spec/04-memory-and-context/sql/memory.sql | Proposed |

Not filed: WS-04's drafted request to add `ecosystem-indexer` / Zoekt indexer as GitHub token-broker callers is covered by accepted **CCR-09-06**. WS-04 dropped the `actions:read` permission it had drafted (CCR-09-06 never grants `actions`); CI-generated contracts are pushed to the indexer with GitHub Actions OIDC instead.

## Orchestrator adjudication — 2026-10-24 (WS-04 requests)

| ID | Decision | Notes |
|---|---|---|
| CCR-04-01 | **Accepted** | Purge deletes all object versions; write-once tombstone export key |
| CCR-04-02 | **Accepted** | `context_assembly_report` artifact type |
| CCR-04-03 | **Accepted** | Memory ID prefixes |
| (orchestrator) | Example model IDs updated to current models | `claude-sonnet-4-5-20250929` retires 2026-11-30 |

## WS-02 — 2026-10-24T16:30Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-02-01 | WS-02 | C-ART §2 (key layout), §4 | Register two Runner-owned control keys under the run's artifacts prefix: `inbox/_head.json` (`{"next_seq": n}`, updated by compare-and-swap with `If-Match`/`If-None-Match: *`, runner-api only) and `inbox/ack-<message_id>.json` (write-once `{"state":"delivered\|rejected","detail","at"}`). Artifact type `steering_control`, `application/json`, 4 KiB, `r90d`, redaction `n/a`. Sandbox session policies already deny writes to `inbox/*` | Sequence allocation for `inbox/<seq:06d>-<message_id>.json` with several runner-api replicas and no database (D-23) needs an atomic counter, and `GET /v1/runs/{run_id}/messages/{message_id}` needs a durable delivery state. spec/02-execution/runner-service.md §6 | Proposed |
| CCR-02-02 | WS-02 | C-DEF §4.3/§6, C-ART §2 (registry bucket) | At bundle build, the registry writes a write-once pointer `bundles/<release_bundle_id>.json` = `{"release_bundle_id","digest":"sha256:<hex>"}` in the `registry` bucket (blob first, then pointer, then row). Fallback if rejected: add optional `release_bundle_digest` to `RunSpec` and CRD | Go services resolve definitions only through the job's bundle (ADR-005) but receive only `release_bundle_id`, which is a truncated hash and cannot be inverted to the blob key. runner-api needs the bundle's `runtime_selection` and `network_profiles` policies at admission, and the controller needs `network_profiles` to render CiliumNetworkPolicy. spec/02-execution/runner-service.md §3.2 | Proposed |
| CCR-02-03 | WS-02 | C-IDS §2 (Job/Pod name row), §7 (sandbox env vars), §8 (labels) | (a) When the controller uses a bare Pod (permitted by C-CRD §6), the Pod name is `ar-<body>` (= AgentRun name). (b) Add env vars `REPLICANT_WORKSPACE_DIR` and `REPLICANT_WORKSPACE_LIMIT_BYTES`. (c) Add pod labels `replicant.example.com/workspace-enforcement` (C-CRD enforcement value) and `replicant.example.com/workspace-bytes` (decimal bytes) on sandbox Pods, and pod annotation `replicant.example.com/cancel-reason` (C-CRD cancel reason or `deadline`/`none`) | (a) ADR-02-002: deterministic name gives hard at-most-once. (b) WS-03 entrypoint and WS-02 watchdog need the hard limit. (c) The NRI plugin must read enforcement from labels because containerd forwards no annotations to Kata (WS-09 G-12); the entrypoint reads the cancel cause through the downward API (XD-03-04) | Proposed |
| CCR-02-04 | WS-02 | C-ART §4 (artifact type registry), §1 (who may write: add agentrun-controller for this type) | Add type `agentrun_record`: bucket class `artifacts`, key `<run prefix>agentrun_record/agentrun.json`, `application/json`, 64 KiB, `r2y`, redaction `n/a` (the final AgentRun object with `spec.runGrant` removed and conditions' messages truncated), producer agentrun-controller at the terminal phase, consumer runner-api (tombstone for `410 run_expired`) and auditors | C-RUNNER requires `410 run_expired` after AgentRun GC, but the Runner has no database (D-23); the record is the tombstone and a durable audit copy of execution state. spec/02-execution/controller.md §7.1 | Proposed |
| CCR-02-05 | WS-02 | C-CRD §4 (failure reasons), C-RUNNER `FailureReason` | Add failure reason `DefinitionUnavailable` (phase reached via Pending, retryable: yes): the registry blob for a pinned definition or bundle policy could not be fetched after the controller's retry budget | Today this surfaces as `ControllerError` (which also pages for controller bugs); separating an S3/registry outage from controller defects improves alerting and retry policy. Low priority; `ControllerError` with `Accepted=False/DefinitionFetchFailed` is the fallback | Proposed |

## WS-05 — 2026-10-24T14:12Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-05-01 | WS-05 | C-ART §4 (artifact type registry) | Add type `qa_generated_flow`: bucket class `artifacts`, key `<run prefix>qa_generated_flow/flows.tar.zst`, content type `application/x-tar+zstd` (Playwright `*.spec.ts` files written by the QA agent, `flows.json` index, lint results), max 4 MiB, retention `r1y`, redaction `scan-block` (generated code; secret-scanned, not altered), producer QA run, consumers UI/approval packet/teams copying flows into repos | Agent-generated smoke flows are evidence the approver must be able to read ("what exactly was checked"), and no existing type fits (`qa_report` is JSON results, `playwright_trace` is a zip of execution traces). spec/05-qa/headless-validation.md §5, artifacts.md §1. Fallback if rejected: embed flow sources (≤ 64 KiB total) inside `qa_report` checks and drop larger flows | Proposed |
| CCR-05-02 | WS-05 | C-PG §2 (schemas table) | Add schema `qa` in database `replicant`: writer **replicant-projector** (activity `qa.index_results` on `replicant-projections`), DDL owner WS-05, readers replicant-worker (classification), control-plane-api (UI flake views), `grafana_ro` (aggregate view `qa.flake_summary_daily` only). Tables `qa.test_results` (monthly partitions, 120-day retention), `qa.indexed_runs`, `qa.test_stats_daily`, view `qa.test_stats_30d`; RLS on `tenant_id`. DDL: spec/05-qa/sql/qa-schema.sql (applied cleanly to PostgreSQL 17 on 2026-10-24) | Telling flaky tests from regressions needs per-test history across jobs (spec/05-qa/failure-feedback.md §4.3, ADR-05-005). The data is derived from `qa_report` artifacts in S3 and rebuildable, matching the projector's role as writer of derived read models. Fallback if rejected: put the same tables in `jobs_read` (also projector-written), or classify from in-run evidence plus quarantine only (loses the flaky_known/flaky_new split for unquarantined tests) | Proposed |
| CCR-05-03 | WS-05 | C-IDS §11 (Prometheus label keys) | Add allowed label key `qa_stage` (closed set of 22 values: `prepare`, `install`, `unit`, `integration`, `build`, `image_build`, `sbom`, `secret_scan`, `services`, `stubs`, `seed`, `app_start`, `health`, `smoke`, `routes`, `e2e_repo`, `e2e_generated`, `contract_compat`, `a11y`, `visual`, `shutdown`, `agent`) | Metrics `replicant_qa_stage_duration_seconds` and `replicant_qa_checks_total` need a per-stage breakdown; bounded cardinality (22 × tenants). spec/05-qa/failure-feedback.md §7. Fallback: one metric name per stage group | Proposed |

## Orchestrator adjudication — 2026-10-24 (WS-05 requests)

| ID | Decision | Notes |
|---|---|---|
| CCR-05-01 | **Accepted** | `qa_generated_flow` artifact type |
| CCR-05-02 | **Accepted** | `qa` schema, writer replicant-projector |
| CCR-05-03 | **Accepted** | `qa_stage` Prometheus label (bounded) |
| OQ-05-04 / XD-05-16 | **Accepted (orchestrator)**: `allowPrivilegeEscalation: true` only on the `builder` sidecar of Kata `qa_build` pods; no credentials in that container. WS-09 narrows the VAP in the Phase 3 fix round |

## WS-07 — 2026-10-24T14:14Z

CCR-07-01 (identity-service as a broker caller) was **not filed separately**: CCR-09-06 (accepted) fully covers it (identity-service `metadata:read`, `members:read`; plus the indexer callers and the global ban on `workflows`/`administration`/`secrets`/`actions`). WS-07 designs against C-ARCH 1.2.0.

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-07-02 | WS-07 | C-MSG 1.2.0 `GateDecideResult.denial_reason` | Add enum values `identity_unverifiable` (Okta live read failed and the replica fallback was not allowed: critical risk or replica older than 5 min) and `identity_recheck_pending` (an Okta event hook flagged the principal and the live reconcile has not cleared it). Additive | The authoritative decision check (identity.md §10) can fail closed for reasons that are not "not in group"; reporting them as `not_in_approver_group` misleads the approver. Fallback until accepted: report `not_in_approver_group` and carry the true reason in the audit evidence and the adapter message | Proposed |
| CCR-07-03 | WS-07 | C-IDS 1.3.0 §4 workflow types | Add system workflow type `ProjectionRebuild` (ID `sys:projection-rebuild:<ulid>`), task queue `replicant-projections` | C-PG rule 5 requires `replicant admin projections rebuild`; a workflow makes it durable, resumable, rate-limited and visible. Fallback: a Kubernetes Job running the same projector code | Proposed |
| CCR-07-04 | WS-07 | C-IDS 1.3.0 §2 identifier catalog | Add prefixes: `art_` (derived: `derived("art", bucket_class, key)`, artifact index rows in `jobs_read.job_artifacts`, used in URLs), `dlg_` (ULID, approval delegations), `opr_` (ULID, admin operation runs, D-26), `lnk_` (ULID, identity links), `grp_` (ULID, SCIM group IDs returned to Okta), `ntf_` (derived from the notification dedupe key) | New identifier types need a CCR (C-IDS §Purpose). All follow the 26-char Crockford rule | Proposed |
| CCR-07-05 | WS-07 | C-IDS 1.3.0 §4 workflow types | Add system workflow type `ScheduledIntake` started by Temporal Schedule actions; one activity `intake.submit_scheduled` that calls `POST /v1/jobs` as `svc_scheduler` with `on_behalf_of` and `dedupe_key = schedule:<sch_id>:<scheduled_time>` | Schedules are owned by Temporal (C-STATE) but `job_id` must be minted in the intake transaction (C-IDS), so the schedule cannot start `ReplicantJob` directly. Fallback: a singleton scheduler loop in control-plane-api reading schedule specs (triggers.md §5) | Proposed |

## Orchestrator adjudication — 2026-10-24 (WS-07 requests)

| ID | Decision | Notes |
|---|---|---|
| CCR-07-01 | **Superseded** by CCR-09-06 | — |
| CCR-07-02 | **Accepted** | New gate denial reasons |
| CCR-07-03 | **Accepted** | `ProjectionRebuild` system workflow |
| CCR-07-04 | **Accepted** | ID prefixes |
| CCR-07-05 | **Accepted** | `ScheduledIntake` system workflow |
| ADR-07-004 | **Accepted**: dev App webhooks default to a Cloudflare quick tunnel (smee re-serializes bodies, breaking signatures). WS-11 updates its default in the fix round |

## Orchestrator adjudication — 2026-10-24 (WS-02 requests and runtime decisions)

| ID | Decision | Notes |
|---|---|---|
| CCR-02-01..05 | **Accepted** | Applied by WS-00 |
| Challenge: D-12 (WS-03 ADR-008) | **Accepted**, with WS-02 evidence (F-6a verified; B-MEM-01 decides per class) | D-12 amended in shared context |
| Default runtime | **Accepted**: Kata 4.3.1 runtime-rs + Cloud Hypervisor for all classes; gVisor standby; Firecracker disabled by policy | WS-02 ADR-02-001 |
| OQ-02-01 nested virtualization in production | **Accepted** with automatic fallback to metal when B-GATE fails | ADR-02-007 |
| OQ-02-02 `guestBlockDevice` via NRI + direct-volume | **Accepted**, gated by B-DISK-01..04 on every Kata upgrade; fallback `xfsProjectQuota` | — |

## Orchestrator adjudication — 2026-10-24 (CCR-02-04 vs CCR-09-07)

- **Decision:** add a third exec role `agentrun-controller` (least privilege: Get/Head on run manifests, conditional Put on `agentrun_record/`). Rejected alternative: reusing the runner-api role (widens a shared credential). WS-09 IAM examples updated in the Phase 3 fix round; WS-10 notified.

## WS-10 — 2026-10-24T16:30Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-10-01 | WS-10 | C-PG rule 8 | Replace "compares `ops.alembic_version` to the revision it was built against. A mismatch prevents readiness" with: "checks that the database revision is **compatible** with the revision it was built against (`replicant_db.revision`: equal, or ahead only by expand/data/reference/grants revisions and by contract revisions whose `requires_reader_revision` ≤ the built revision); otherwise it refuses readiness. Strict equality is allowed only for `REPLICANT_ENV` local/ci." | Expand → migrate → contract across releases (D-26) leaves the DB legitimately ahead of running pods, and pinned replicant-worker versions live up to 37 days (ADR-006, CC-4). Equality would fail every such pod on its next restart. Validated with the worked example (spec/10-data-platform/schema-management.md §7.3, §8) | proposed |
| CCR-10-02 | WS-10 | C-ART §5.3 | Replace "The bucket policy denies `PutObject` without the `replicant-retention` tag or with a KMS key outside the environment's key set" with: "The bucket policy denies a `replicant-retention` value not allowed in the bucket and a KMS key outside the environment's key set **when the header is present** (`…IfExists`). Objects written without SSE headers are encrypted with the environment's quarantine key (default bucket encryption), a whole-bucket backstop rule set to the bucket's longest class bounds untagged objects, and a daily storage audit over S3 Metadata tables flags both." | `CreateMultipartUpload`, `UploadPart`, and `CompleteMultipartUpload` are all authorized as `s3:PutObject`, but only `CreateMultipartUpload` carries tags and SSE headers. Header-must-exist denies would block multipart uploads (diff bundles up to 256 MiB, workspace archives up to 10 GiB). AWS documents the same failure for conditional headers (https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html). The tag/SSE case is inferred, so the staging conformance test settles it; the IfExists design is safe either way | proposed |
| CCR-10-03 | WS-10 | C-RUNNER `/sandbox/v1/credentials/aws` description (and WS-09's sandbox session policy) | Add `s3:PutObjectTagging` on the same run-prefix resources as `s3:PutObject` | "s3:PutObjectTagging Required if you want to put object tagging when you make a PutObject request" (also CreateMultipartUpload) [verified: https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html, 2026-10-24]. C-ART §5.3 requires every PUT to carry `x-amz-tagging`, so as written every sandbox upload is denied | proposed |
| CCR-10-04 | WS-10 | C-IDS §4 (workflow types) | Register workflow type `DataOperation` (admin data fixes as resumable, audited workflows on the owning service's worker; schema-management.md §9.2) and confirm task queue `replicant-retention` (session-state sweeper activities) under the `replicant-<purpose>` rule | D-26 "One-off data fixes … admin API operations": long data fixes must be resumable and auditable, not inline in the migrate Job | proposed |
| CCR-10-05 | WS-10 | C-ART §2 (registry bucket layout), C-DEF §4.1 rule 5 | Add the write-once key `index/<kind>/<scope>/<name>/<version>.json` = `{"digest","published_at","source_commit"}` to the registry bucket. Publish order becomes blob → index (`If-None-Match: *`) → row | After a Replicant DB restore, lost version rows can be rebuilt from S3, and a re-publish can never reuse a version number for different content (412 on the index object). recovery.md §4.2, §8 | proposed |
| CCR-10-06 | WS-10 | C-ART §2/§4, C-IDS §2 | Add artifact types `audit_event_export` (audit bucket, `org/audit-exports/events/<yyyy>/<mm>/<dd>/<hh>/<first_aud_id>.ndjson.zst`, r7y, Object Lock, writer: WS-08 audit exporter) and `deletion_ledger_entry` (audit bucket, `org/deletion-ledger/<yyyy>/<mm>/<dd>/<del_id>.json`, r7y, write-once, writers: deleting services), plus the identifier prefix `del_<ulid>` | Restores must neither lose audit events (1-min export gives audit an RPO independent of DB PITR) nor resurrect deleted data (ledger replay). recovery.md §2–3, lifecycle.md §5; schema spec/10-data-platform/schemas/deletion-ledger-entry.schema.json | proposed |

## WS-08 — 2026-10-24T14:22Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-08-01 | WS-08 | C-IDS 1.3.0 §5 (trace rules, attribute keys) | (a) Add **Rule T-5**: root span `job` has span ID `lowercase_hex(sha256("replicant.job.span.v1:" + job_id))[0:16]` (T-5a); the entrypoint's `sandbox.run` span has span ID `lowercase_hex(sha256("replicant.run.span.v1:" + run_id))[0:16]` (T-5b). Components acting for a job without an inbound `traceparent` (github-integration on `pr.event`, sweepers, audit export, agentd and the gateway shim when a CLI sends none) parent under these. Test vectors: `job_01m5p7x7r0bt69j0h0fbxepw90` → `2f9b8c2c87b35ba4`; `run_gj3h3ep3phjfs9e23v768zrca5` → `b776c51d0276da4e` (spec/08-observability/code/trace_ids.py). (b) Add span attribute keys `replicant.workflow_type`, `replicant.requester_id` (principal ID), `replicant.cli`, `replicant.request_id` (`gwu_`), `replicant.retry_attempt`, `replicant.memory_entry_ids`, `replicant.phase` to the §5 table | A job lives up to 30 days; header-less work otherwise creates orphan roots in Tempo and duplicate trace rows in Langfuse; Langfuse trace name, user and tags (C-IDS §10) need workflow type and requester on every span (telemetry-model.md §1) | Proposed |
| CCR-08-02 | WS-08 | C-IDS 1.3.0 §11 (Prometheus labels) | Allow `model` (gateway model ID, bounded by bundles' model sets), `cli` (`claude-code`, `claude-agent-sdk`, `codex`), `node_key` (bounded by NodeCatalog), `rule` (redaction rule ID, ≤ 250), `sink` (`s3`, `loki`, `langfuse`, `tempo`, `stdout`), `route`, `method`, `status_code` (HTTP server metrics, templated routes), `kind`, `stage`, `source`, `type` (bounded enums named per metric in telemetry-model.md §8), and `pod` on the `replicant_sandbox_*` and `replicant_cli_*` families only. Clarify that native labels of third-party and SDK metric families (Temporal, controller-runtime, cAdvisor, kube-state-metrics, Loki, Alloy) are kept, except the forbidden identifiers | Model cost breakdowns, redaction findings per rule and sink, HTTP SLO (route/status), and per-pod sandbox resource gauges (no cross-process delta merging) need these; all bounded | Proposed |
| CCR-08-03 | WS-08 | C-RUNNER 2.1.0 (`RunSpec`), C-CRD 2.1.0 (`spec`), C-IDS 1.3.0 §7 (sandbox env) and §8 (labels) | (a) Optional immutable `RunSpec.workflow_type` → `spec.workflowType` (C-IDS §4.1 values). (b) Controller sets pod labels `replicant.example.com/workflow-type` (from `spec.workflowType`) and `replicant.example.com/acting-for` (the `acting_for` principal ID from the **verified** `spec.runGrant`; ≤ 30 chars). (c) Sandbox env `REPLICANT_WORKFLOW_TYPE`, `REPLICANT_WORKFLOW_ID`, `REPLICANT_LOGICAL_ACTIVITY_ID` | Sandbox telemetry identity comes only from pod labels (ADR-08-004); Loki `workflow_type` label and Langfuse `langfuse.user.id`/trace name for sandbox spans need them; the entrypoint needs node key and cycle for observation metadata | Proposed |
| CCR-08-04 | WS-08 | C-ART 1.3.0 §2, §4, rule 4; C-IDS 1.3.0 §2, §4 | (a) Artifact types in bucket class `audit`: `workflow_history_export` (`tenants/<t>/jobs/<job_id>/history/<temporal_run_id>.json.zst`, `application/json`+zstd, 64 MiB, r2y default or r7y by tenant retention policy, redaction n/a (codec-encrypted payloads)); `audit_partition_export` (`org/audit-exports/events_YYYY_MM.ndjson.zst` + `.manifest.json`, 4 GiB, r7y, Object Lock); `okta_system_log_export` (`org/okta-system-log/YYYY/MM/DD/<firehose object>`, r7y); `leak_purge_record` (`tenants/<t>/jobs/<job_id>/purges/<leak_id>.json`, old key → new key and sha256 per object, r7y). (b) Amend rule 4: a leak purge may write a superseding key `<original name>.p<n>` and delete all versions of the original through role `replicant-leak-purge`; readers resolve supersession through the job's `purges/` records. (c) New workflow type `LeakPurge`, ID prefix `leak_<ulid>` (= workflow ID), task queue `replicant-ops` | Audit retention beyond Temporal's 30-day closed retention (archival is experimental), Okta's 90-day log retention, and a purge path that keeps C-ART's write-once invariant auditable (session-capture.md §8, audit.md §1) | Proposed |
| CCR-08-05 | WS-08 | C-ART 1.3.0 §1 | State that the seven bucket classes cover artifacts only; observability backend stores use separate buckets `<org>-replicant-<env>-obs-{loki,loki-ruler,tempo,mimir-blocks,mimir-ruler,mimir-alertmanager,langfuse,clickhouse-backup}` (SSE-KMS `alias/replicant/<env>/observability`) | Avoid ambiguity with "one bucket per (environment, data class)" (platform.md §1.3) | Proposed |

## Orchestrator adjudication — 2026-10-24 (WS-10 requests)

| ID | Decision | Notes |
|---|---|---|
| CCR-10-01 | **Accepted** | D-26 amended: compatibility, not equality |
| CCR-10-02 | **Accepted** | Multipart-safe bucket enforcement |
| CCR-10-03 | **Accepted** | `s3:PutObjectTagging` for sandbox uploads |
| CCR-10-04 | **Accepted** | `DataOperation`, `replicant-retention` |
| CCR-10-05 | **Accepted** | Write-once registry version index |
| CCR-10-06 | **Accepted** | Audit export / deletion ledger types, `del_` |

## Orchestrator adjudication — 2026-10-24 (WS-08 requests)

| ID | Decision | Notes |
|---|---|---|
| CCR-08-01..05 | **Accepted** | Applied by WS-00 |
| OQ-08-01 Loki tenancy | **WS-08 position accepted**: tenancy by data class with enforced control-plane-api query proxy and no direct tenant access (satisfies WS-09 G-08). WS-09 tenancy.md §8 to align in the Phase 3 fix round |
| OQ-08-02 Langfuse Enterprise | **Open for the user** (license vs OSS workarounds) |

## Orchestrator adjudication — 2026-10-24 (WS-00 follow-ups on WS-10/WS-08 batch)

- **Leak purge vs Object Lock (rule 4a):** keep `s3:BypassGovernanceRetention` for `replicant-leak-purge`, scoped to `tenants/*/jobs/*/history/` only, every bypass recorded, **plus mandatory two-person approval** of the `LeakPurge` workflow before the bypass step. Alternative (crypto-shred the job payload data key) recorded for R2 review. WS-08/WS-09 confirm in the fix round.
- **Overlapping audit exports** (`audit_event_export` per-minute vs `audit_partition_export` monthly): WS-08 confirms in the fix round whether both are needed.

## WS-01 — 2026-10-24T15:40Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-01-01 | WS-01 | C-MSG 1.2.0 (`ReleaseNoticeSignal.kind`, `JobSnapshot.pending_notices.items.enum`), events-and-signals.md §2.2 | Add enum value `bundle_patched` (additive). Semantics: sent after operators moved workflows pinned to build N onto replay-verified emergency patch build N.p with `UpdateWorkflowExecutionOptions`; `recommended_release_bundle_id` = the patch bundle B' (B with only `worker_deployment` and `node_catalog` changed, `lineage.parent_release_bundle_id = B`). The workflow verifies the lineage (activity) and records B' as its bundle without consent | ADR-006 allows emergency fixes "in a new build of the same version line"; Temporal versions are immutable, so a patch is a new build ID. Without this notice a patched job would run on a build that is not in its recorded bundle, violating D-03. spec/01-orchestration/adr/003-patching-only-for-emergency-patch-builds.md. Fallback if rejected: patched jobs keep their old `release_bundle_id` and the patch is recorded only in audit (D-03 gap) | Proposed |
| CCR-01-02 | WS-01 | C-IDS 1.3.0 §6 (phase vocabulary) | Add phases `pre_checks` (inserted pre-implementation checks, e.g. license scan), `fan_out` (MaintenanceBatch), `budget_gate` (budget extension wait or tenant quota wait), `migrating` (handoff run), `takeover` (takeover subflow). Additive; `job_phase` pattern already admits them | Phases are projected and shown to users (graphs/README.md §8); the vocabulary is closed by convention and WS-07 renders it | Proposed |
| CCR-01-03 | WS-01 | C-ART 1.3.0 §4 (artifact types) | (a) `feedback_triage`: bucket `artifacts`, key `tenants/<t>/jobs/<j>/steps/triage.c<c>.n<o>/feedback_triage/triage.json`, `application/json`, 2 MiB, r1y, redaction `required` (quotes reviewer comments), producer activity `feedback.triage`, consumers repair run inputs, UI, memory extraction. (b) `node_report`: bucket `artifacts`, key `…/steps/<node>.c<c>.n<o>/node_report/<name>.json`, `application/json`, 16 MiB, r2y, redaction `required`, producer auxiliary node activities (`checks.run_step`: contract compatibility, consumer impact, docs scope; inserted check nodes such as `license_policy_scan`), consumers approval packet, UI | Every artifact type must be registered (C-ART §4). A generic `node_report` type lets new auxiliary node types (code path of D-02) ship without a contract change; `attributes.report_kind` distinguishes them | Proposed |
| CCR-01-04 | WS-01 | C-MSG 1.2.0 §2.2 + schemas (new `BatchChildProgressSignal`), C-IDS §4 message names | Add Signal `batch.child_progress` on workflow type `MaintenanceBatch`. Sender: child `ReplicantJob` workflows via `SignalExternalWorkflowExecution` (a workflow command; no client credential). Payload `{schema: "replicant.msg.batch-child-progress/v1", child_job_id (job_id), status (job_status), phase (job_phase), seq (integer ≥ 1)}`, `additionalProperties: false`. Dedupe and ordering: the parent keeps the highest `seq` per child | Children of a maintenance sweep must free concurrency slots when they enter PR review and report terminal status, but they are started with `ParentClosePolicy.ABANDON` (the parent continues-as-new), so child-completion events are not available. spec/01-orchestration/adr/007-maintenance-fan-out-as-abandoned-child-jobs.md. Fallback if rejected: the parent polls visibility (`ReplicantParentJobId = <job_id>`) every 5 min through an activity | Proposed |
| CCR-01-05 | WS-01 | C-MSG 1.2.0 `ProjectionEvent`, C-STATE §2.1 | **Optional, efficiency.** Allow `ProjectionEvent.events` (array of 1..20 `TimelineEvent`, each with its own `seq`) in addition to `event`, carrying all transitions of one workflow task with one snapshot (the latest `seq`). The projector inserts one timeline row per element (`ON CONFLICT DO NOTHING`) and applies the snapshot with the existing seq guard. Additive: `event` stays required for v1 senders; projectors must accept both | Measured: projection activities are 36 % of all activities in a 5-cycle job and every one carries a full JobSnapshot, so they dominate history bytes (spec/01-orchestration/history-budget.md). Coalescing per workflow task cuts activity count and bytes without changing projection semantics. Not required for correctness | Proposed (optional) |

### Orchestrator adjudication: WS-01 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-01-01 `bundle_patched` notice | **Accepted** | Closes the D-03 gap for emergency patch builds. Lineage check is an activity; B' may differ from B only in `worker_deployment` and `node_catalog` |
| CCR-01-02 phases | **Accepted** | WS-07 must render the five new phases |
| CCR-01-03 `feedback_triage`, `node_report` | **Accepted** | `node_report` requires `attributes.report_kind`; WS-04 consumes `feedback_triage` (redacted) |
| CCR-01-04 `batch.child_progress` | **Accepted** | Sender is a workflow command, no client credential; WS-09 to note it in G-23 enumeration |
| CCR-01-05 batched ProjectionEvent | **Accepted (optional)** | Projectors (WS-07) must accept both `event` and `events`; senders may keep `event` |

ADR-01-001 (Challenge to D-01 mechanism): **Accepted.** D-01 (Temporal + LangGraph) stands; LangGraph runs
inside the workflow with each side effect as an explicitly-IDed activity, behind a GraphEngine adapter with
an interpreter fallback, instead of the `temporalio-langgraph` plugin's activity-per-node model.
V06-14 verified by WS-01 (V01-01): the one-new-build-ramp limit is lifted.

## WS-09 — 2026-10-24 (fix round wave 1)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-09-08 | WS-09 | C-ARCH 1.4.0 §4 (`agentrun-controller` role) and §1.3/§4 (KMS list, deployment diagram) | (a) Replace "`s3:HeadObject` (with `ChecksumMode`)" with "`s3:GetObject` (S3 authorizes HeadObject as GetObject), on every object under the run's three prefixes" and state that this role can therefore read run content; (b) replace the separate "temporal-codec" KMS key with "per-tenant keys plus `replicant-system` with encryption context `replicant:purpose=temporal-payload`"; (c) mark `replicant-kata-fc` "defined, disabled by policy" in the deployment diagram | (a) There is no `s3:HeadObject` IAM action; HeadObject requires `s3:GetObject` [verified: https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html, 2026-10-24], so the contract text cannot be implemented literally and understates the role's reach (R1-048, threat-model §7.3). (b) R1-214. (c) R1-230 | Proposed |
| CCR-09-09 | WS-09 | C-CRD 2.3.1 §1 (phase diagram, rule 4) | Add: "When `cancelReason=security`, every terminal transition derives reason `SecurityViolation` (non-retryable), including `Cancelling → Failed` on node loss" and remove `Cancelling → Failed: NodeLost` for that case | A node is usually replaced after a security cordon; deriving `NodeLost` makes the run retryable and skips grant revocation (R1-223) | Proposed |
| CCR-09-10 | WS-09 (with WS-07) | C-ART 1.9.0 §1 (buckets outside the seven classes, like CCR-08-05) and §4 | Register bucket `<org>-replicant-<env>-ops` (versioned, no Object Lock, SSE-KMS `alias/replicant/<env>/ops`, writer control-plane-api only) and artifact type `kill_switch_state` (`kill-switches/v1/state.json`, `application/json`, 64 KiB, perm (noncurrent versions 400 d), n/a redaction: enums, IDs and timestamps only) | Out-of-band kill-switch mirror required by C-MSG 1.5.0 `ops.read_kill_switches` and R2-016; readable when Postgres is down (secrets.md §12) | Proposed |
| CCR-09-11 | WS-09 | C-IDS 1.11.0 §8 (Kubernetes labels) | Add namespace labels `replicant.example.com/zone` (`sandbox`, `system`), `replicant.example.com/tenant-ready` (`"true"`/`"false"`), `replicant.example.com/exec-kill-switch` (`"true"`/`"false"`), `replicant.example.com/max-active-runs` (decimal), `replicant.example.com/isolation-group` (`shared-standard` or `iso-<tenant_id>`) | Controller watch, runner-api admission, the Cilium baseline and the admission policies all key on these; none is registered (R1-031) | Proposed |

## WS-04 — 2026-10-24T16:30Z (wave-1 fix round)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-04-04 | WS-04 | C-IDS §2 (`memory_snapshot_id`) | Hash body becomes `JCS({"tenant_id": <tenant>, "entries": <sorted entry refs>, "policy": <pinned policy ref>})`: `msnap_<b32_130(sha256(…))>`. Format and regex unchanged | R1-096 (blocker): without the tenant, two tenants with the same eligible set (only org entries, or empty) produce the same ID; the second insert fails on the primary key and RLS hides the first tenant's row. Evaluation snapshots hash the scope (source) tenant | Proposed |
| CCR-04-05 | WS-04 | C-JOBREQ `$defs.evaluation_options` | Add optional `memory_replay_job_id` (`^job_[0-9a-hjkmnp-tv-z]{26}$`): the source job whose recorded memory an eval job replays exactly. Valid only with `mode: eval` and `memory_snapshot_id`, which must equal the source job's snapshot (memory-service verifies) | R1-101 / R1 §5.1 (must-answer 4): snapshots are shared across jobs, so the snapshot ID alone cannot identify the source runs whose injections (refs, order, renderer version) and pull results must be replayed | Proposed |
| CCR-04-06 | WS-04 | C-ART §2 rule 5 | Add (iii): on an admin or leak purge, memory-service may delete all object versions of `memory_export` objects (`tenants/<t>/exports/<export_id>/*`, `org/exports/<export_id>/*`) whose manifest lists a purged entry; audited | R1-105: exports (r1y) hold verbatim memory text; rule 5 today names only `memory_snapshot_export` and `memory_injection`, so a `secret_leak`/`pii` purge leaves export copies for up to a year. Until accepted, propagation reports `memory_export = requires_break_glass` | Proposed |
| CCR-04-07 | WS-04 | C-PG §2 (`ecosystem` readers) | Add `grafana_ro` as a reader of `ecosystem` **aggregate views only**: `ecosystem.v_source_freshness`, `ecosystem.v_graph_stats` (no table grants) | R1-107 / XD-10-19: freshness dashboards need source and graph health; the views expose no repository content, services, or edges individually | Proposed |

### Orchestrator adjudication: WS-09 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-09-08 HeadObject → `s3:GetObject`; drop separate temporal-codec key; mark `replicant-kata-fc` disabled | **Accepted** | The controller already reads manifests, so `s3:GetObject` adds no new trust boundary. Its scope stays limited to the three run prefixes |
| CCR-09-09 every terminal path after a security cancel is `SecurityViolation` | **Accepted** | |
| CCR-09-10 ops bucket and `kill_switch_state` type | **Accepted** | |
| CCR-09-11 namespace labels in C-IDS §8 | **Accepted** | |

To be applied by WS-00 together with the other wave-1 CCRs.

### Orchestrator adjudication: WS-04 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-04-04 tenant in `msnap_` hash | **Accepted** | Fixes R1-096 cross-tenant collision |
| CCR-04-05 `options.evaluation.memory_replay_job_id` | **Accepted** | Replay re-renders recorded entries and checks sha256; no cross-tenant read |
| CCR-04-06 purges may delete `memory_export` | **Accepted** | |
| CCR-04-07 `grafana_ro` on two ecosystem aggregate views | **Accepted** | Views only, no table grants |

Note on R2-028: two flags from distinct viewers may put an entry on hold. This is a tightening-only action, consistent with D-25 (only admins create, edit, or activate memory). Abuse by flagging is bounded by the admin SLA review.

## WS-06 — fix round wave 1 — 2026-10-24T17:40Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-06-07 | WS-06 | C-JOBREQ 1.2.0 (`$defs.evaluation_options`); C-RUNNER 2.4.0 admission text; C-DEF 2.4.0 §6; C-IDS §4 | (a) Add optional `options.evaluation.harness_task = {kind: score \| snapshot_build \| compat_check, spec_uri}` (mode `eval` only): the ReplicantJob skips router and graph and runs only the harness steps (`eval_score.c0.n1.run`, `golden_snapshot.c0.n1..n5.run`, `compat_check.c0.n1..n9.run`). (b) Add optional `options.evaluation.pipeline_id` (`^rpl_…$`), required when `mode = shadow`; control-plane-api registers the job with gateway budget account `fgw:pipeline:<pipeline_id>`. (c) runner-api admission (and C-DEF §6 "resolve only through the job's bundle") gains one exception: a run with `role: eval` in tenant `replicant-eval` may reference a **global** AgentDefinition labelled `replicant.example.com/eval-toolchain: "true"` that is outside the job's bundle closure; the environment definition must still come from the closure. (d) C-IDS §4: workflow type `ComponentSuite` (eval-worker, `replicant-eval`, ID `eval_<ulid>`) | R1-134: eval scoring, snapshot and compatibility runs cannot form a valid RunSpec from eval-worker (no `job_…` workflow, no live callback, no grant signer). Owning them by a real harness job fixes all four. A scorer inside the evaluated bundle would score the two arms differently, hence (c). (b) is R2-061; (d) is R1-151 | Proposed |
| CCR-06-11 | WS-06 | C-MSG 1.4.0 `ReleaseSignoffRequest.signoff_point` | Add enum values `tenant_layer_behavioral`, `cost_acceptance`, `judge_acceptance`, `shadow_extension`, `bootstrap_default` | R1-145: behavioral tenant layers, G-OFF-6/7/8 `needs_signoff`, the G-SH-1 extension, and environment bootstrap all need a human decision with no sign-off point in the frozen enum | Proposed |
| CCR-06-12 | WS-06 | C-DEF 2.4.0 §4.4 slot/state table; §4.3 derived-bundle timing | Add rows: (1) shadow job (`options.evaluation.mode = shadow`), `requested_release_bundle_id`, requester `svc_release-controller` with `on_behalf_of`: accepts `shadowing` only (the candidate or its derived bundle for the job's tenant); (2) any other explicit `requested_release_bundle_id` in a live tenant: `promoted`, `deprecated`. Add the rule that every slot refuses bundles marked `eval_only` outside `replicant-eval`. State that derived bundles are built when the base enters `shadowing`, `canary` or `promoted` | R1-133 (shadow jobs could not pin), R1-137c (live-tenant pins of canary bundles are not in the table), R1-151 (eval-only bundles) | Proposed |
| CCR-06-13 | WS-06 | C-MSG 1.4.0 (`ReleaseNoticeSignal.kind`, `GateDecideRequest`, `GateView`, `OfferBundleMigrationRequest`) | (a) Notice kind `bundle_rolled_back`. (b) `GateDecideRequest.override_release_block = {reason (3–2000 chars)}` optional; new handler outcome: an `approve` on a gate whose job's bundle was rolled back or revoked is rejected with `GateBlockedByRelease` unless the override is present (audited); `reject` and `request_changes` are never blocked. (c) `GateView.release_block = {release_bundle_id, rolled_back_at, actor, reason}` optional. (d) `OfferBundleMigrationRequest.consent` ∈ {`requester` (default), `operator`}; `operator` requires `release:revoke`, opens no `bundle_migration` gate, and migrates at the next safe point; the job's requester is notified | R2-021: after a rollback, gates on the bad bundle stay approvable with no marking, and operator-forced migration does not exist (requesters on leave block a safety migration) | Proposed |
| CCR-06-14 | WS-06 | C-ART 1.9.0 §2 (eval bucket layout), §4 (types) | Add eval-bucket keys `pipelines/<pipeline_id>/evidence/<signoff_point>-r<revision>.json` (new type `release_evidence_packet`, `application/json`, 4 MiB, retention `perm` for promoted bundles else `r2y`, redaction n/a) and `anchors/<anchor_set>/<item>.json` (new type `judge_anchor_item`, `application/json`, 8 MiB, `perm`, redaction required) | R1-144 (no evidence-packet artifact or digest rule), R1-156 (`anchors/` prefix not in C-ART) | Proposed |
| CCR-06-15 | WS-06 | C-PG 1.2.0 §2 reader columns | Add `eval-worker` as a SELECT reader of `jobs_read`, `intake` (`job_requests` only), `approvals`, and `cost` | R1-285: online lagging metrics (`sys:online-metrics`) and memory outcome counters read these schemas; no cross-owner view is created inside `evals` | Proposed |
| CCR-06-16 | WS-06 | C-DEF 2.4.0 §4.3 example request | Replace the example with a valid `ReleaseBundleRequest` (`spec.mode: full`, `spec.changeSummary`, fields under `spec.full`), i.e. `06-releases-and-evals/config/bundle-requests/main.yaml` | R1-156: the contract's example does not validate against the request schema WS-06 owns | Proposed |

## WS-03 fix round, wave 1 — 2026-10-24T17:19Z

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-03-05 | WS-03 | C-STATE §1 (new row) | Add row **"Per-run gateway policy (models, budget_usd, max_concurrency, expires_at)"**: owner T (the run plan in workflow state); writer replicant-worker (`agents.stage_inputs`); derived copy in Valkey `gateway-state` (`fgw:run:<run_id>:policy`); readers replicant-gateway-shim; consistency: idempotent PUT, rebuilt by re-registration from the workflow's run-wait loop; retention: run lifetime | R1-286: the policy existed only in Valkey, which C-STATE describes as ephemeral counters, so data loss made every in-flight run fail permanently with no rebuild owner | Proposed |
| CCR-03-06 | WS-03 | C-ART §2, §4 | Register (1) `agent_prompt`: bucket `artifacts`, `text/markdown`, 1 MiB, `r90d`, redaction **required** (contains issue text), producer context assembler (`context.stage_inputs`) → entrypoint (`inputs/system-append.md`, `inputs/first-message.md`); (2) `gateway_usage_log`: bucket `artifacts`, key `platform/gateway-usage/<yyyy>/<mm>/<dd>/<hh>/<pod>-<seq:010d>.ndjson.zst` (a non-tenant platform prefix), `application/x-ndjson` zstd, 64 MiB per object, retention `r7d`, redaction n/a (usage metadata only, no content), producer replicant-gateway-shim → usage-ingestor; (3) clarify that `inputs/run-plan.json` is a listed object of type `run_inputs` (producer `agents.build_run_plan`) | R1-067 (composed prompt files need a type) and R1-077 / XD-10-13 (durable ≥ 72 h usage retention). Fallback until accepted: prompt files typed `task_packet`; usage log in the `audit` bucket is NOT an acceptable fallback, so the shim keeps an in-memory queue and loses replayability | Proposed |
| CCR-03-07 | WS-03 | C-ARCH §1.1 (`replicant-gateway-shim` row), §6 | Replace "Valkey `gateway-state` (primary + replica, Sentinel ×3)" with "Amazon ElastiCache for Valkey `gateway-state` (1 primary + 1 replica, Multi-AZ, IAM AUTH, no snapshots; ephemeral)"; add "writes the durable usage log to S3 `artifacts` `platform/gateway-usage/`"; list gateway callers: sandboxes (run tokens), replicant-worker, memory-service, eval-worker (projected SA tokens, purpose-bound) | R1-085 (WS-10 provisions ElastiCache), R2-057 (embeddings), R1-150 (eval judges) | Proposed |
| CCR-03-08 | WS-03 | C-RUNNER `/sandbox/v1/token:refresh` description (wording erratum) | "The entrypoint refreshes … and rewrites `$REPLICANT_RUN_TOKEN_DIR/<audience>`" → "**replicant-agentd** (the token custodian; the agent container holds no token) refreshes … and rewrites the token files in its private directory `$REPLICANT_RUN_TOKEN_DIR` (agentd's tmpfs copy of the projected Secret)" | R1-092: C-ARCH and XD-09-01 make agentd the sole holder; the old wording implied the agent-container entrypoint holds tokens | Proposed |
| CCR-03-09 | WS-03 | C-IDS §7 (headers) | Add `X-Replicant-Served-Model` (shim → caller, every data-plane response; trailer for streams), `X-Replicant-Purpose` (service caller → shim: `worker_llm`, `embedding`, `eval_judge`), `X-Replicant-Pipeline-Id` (service caller → shim, ≤ 64 chars, backfill or release-pipeline counter) | R1-150, R2-057, R2-061 | Proposed |

## WS-02 — 2026-10-24T18:45Z (fix round wave 1)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-02-06 | WS-02 | C-CRD 2.3.1 `spec.priorityClassName` enum; C-RUNNER translation text | Add `replicant-eval` to the enum (`[replicant-run-low, replicant-run-normal, replicant-run-high, replicant-run-urgent, replicant-eval]`); runner-api maps `role=eval` / tenant `replicant-eval` runs to it. WS-02 ships PriorityClass `replicant-eval` (value 100, `preemptionPolicy: Never`, below `replicant-run-low`) | Eval batches must never preempt or starve tenant runs (WS-06 XD-06-09). Until accepted, eval maps to `replicant-run-low` (runner-service.md §3) | Proposed |
| CCR-02-07 | WS-02 | C-RUNNER 2.4.1 `getRunAwsCredentials` description | Add to the session policy: `s3:GetObject` on the run's own `<artifacts prefix>manifest.json` (uploader read-back verification), and an explicit `Deny` of `s3:PutObject*` on `<artifacts prefix>agentrun_record/*` and `<artifacts prefix>inbox/*` (today only implied by "except inputs/ and inbox/"). Packed policy stays ≤ 2,048 chars (measured 1,747) | A sandbox must not pre-empt the controller's write-once tombstone (R1-049) or the cancel control key (R2-040); the uploader verifies the manifest it wrote. runner-service.md §7 | Proposed |
| CCR-02-08 | WS-02 | C-IDS §7 (sandbox env vars), §8 (labels/paths) | Register env vars `REPLICANT_RUN_TOKEN_SEED_DIR` (=`/run/replicant/token-seed`, agentd only), `REPLICANT_RUN_TOKEN_DIR` (=`/run/replicant/tokens`, agentd only), `REPLICANT_UPLOADER_SOCKET` (=`/run/replicant/uploader-ipc/agentd.sock`, agentd and uploader), `REPLICANT_S3_VERIFY_READBACK` (`true`\|`false`, uploader; `false` only in runner-local), `OTEL_EXPORTER_OTLP_CERTIFICATE` (=`/etc/replicant-ca/ca.crt`); pod labels `replicant.example.com/workflow-type`, `replicant.example.com/acting-for`, `replicant.example.com/network-profile`; AgentRun annotation `replicant.example.com/runtime-override`. Namespace labels are registered by CCR-09-11 (WS-02 concurs) | Token custody only in agentd (G-01, CCR-09-01) and the private uploader channel (R1-042) need named, reserved paths; labels feed CNP selection and WS-08 attribution (R1-041, R2-045) | Proposed |
| CCR-02-09 | WS-02 | C-RUNNER 2.4.1 (paths), C-ART §2/§4 (keys and types) | (a) C-RUNNER: `POST /sandbox/v1/heartbeat` (sandbox listener, run token audience `runner-sandbox`; response `{continue: bool, cancel_reason: string\|null}`), `GET /v1/revocations?since=<cursor>` (orchestration listener; response `{revoked: [{run_id, job_id, at, reason}], next_cursor}`), `POST /internal/v1/runs/{run_id}:revoke` (internal listener, controller only); add verifier obligation "reject tokens whose `run_id` appears in the exec revocation feed". (b) C-ART: Runner-owned control key `<artifacts run prefix>inbox/_control.json` (type `steering_control`, write-once `{cancel_reason, at}`, writers runner-api and agentrun-controller) and key `exec-revocations/<env>/<yyyymmddhh>.ndjson` in the `audit` bucket (type `exec_revocation_feed`, writer runner-api, `r90d`) | A partitioned sandbox keeps spending while its retry runs (R2-030); the cancel cause must reach the agent without exec or annotations into Kata (R2-040, G-12, G-26). runner-service.md §6, §7.3 | Proposed |
| CCR-02-10 | WS-02 | C-ARCH 1.4.0 §4 (`agentrun-controller` role), complements CCR-09-08 | Replace the role's bullets with: `s3:GetObject` on every object under the run's three prefixes (`artifacts`, `transcripts`, `session`; covers HeadObject, per CCR-09-08); `s3:GetObject` on `registry` `blobs/sha256/*` and `bundles/*`; `s3:PutObject` (required `If-None-Match: *`) and `s3:PutObjectTagging` on `…/runs/<run_id>/agentrun_record/*` **and `…/runs/<run_id>/inbox/_control.json`**; KMS `kms:Decrypt` on tenant keys and the `registry` key, `kms:GenerateDataKey` on tenant keys, all with `kms:ViaService` S3; explicitly no `s3:ListBucket`, no `s3:DeleteObject*`, no `s3:BypassGovernanceRetention`, no access to `audit`, `memory`, `eval` | The controller writes the `deadline` cancel control key (R2-040) and every PutObject carries `x-amz-tagging` (CCR-10-03); the exact list is in controller.md §9b and XD-FIX-R1-048 | Proposed |
| CCR-02-11 | WS-02 (R1-065) | C-ARCH 1.4.0 §2 invariant 2 | Add "the sandbox OTLP collector (`otel-sandbox.<env>.replicant.internal:4318`, WS-08 `alloy-sandbox-otlp`)" to the list of services sandboxes reach | WS-02, WS-08 and WS-09 all allow this destination; the invariant says "exactly" and omits it. network.md §2.2 | Proposed |
| CCR-02-06 (correction 18:50Z) | WS-02 | — | PriorityClass `replicant-eval` has `value: 50` (not 100), `preemptionPolicy: Never`, as shipped in 02-execution/manifests/capacity-reserve.yaml; the rest of CCR-02-06 is unchanged | Typo in the row above | Proposed |

## WS-05 — 2026-10-24T17:45Z (wave-1 fix round)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-05-04 | WS-05 (with WS-07, WS-01) | C-MSG gate decisions (`gate_decision.decision` enum) and validation rule V5 | Add decision `rerun_qa`. Valid only on gate kind `publish_approval` when the presented packet's `qa.verdict == "inconclusive"` and `qa.actions` contains `rerun_qa` (V5 extension: otherwise reject `decision_not_offered`); at most 2 per gate. Effect: supersedes the gate revision; the workflow re-runs QA on the same diff bundle and `base_sha` without consuming fix iterations, then opens a new revision. No new fields; `comment` optional | R2-027: today an approver facing "QA inconclusive" (environment broken, then fixed by operators) can only approve blind, reject good work, or misuse `request_changes`, which sends the implementer to "fix" code that is not broken. Fallback until accepted: `request_changes` with `reason_code: "qa_rerun"` (XD-FIX-R2-027) | Proposed |
| CCR-05-05 | WS-05 | C-ART §4 `secret_scan_report` (producer column) | Add the QA run (role `qa`, image build with `secretScan`) as a producer of `secret_scan_report` at `runs/<run_id>/secret_scan_report/image.json`, same schema `replicant.secret-scan-report/v1` and retention r2y as WS-09's scanner | R1-126: QA already writes this type (accepted CCR-09-05 registered it for the scanner only), so the uploader's type/role allowlist would reject it | Proposed |

## WS-11 — 2026-10-24T18:00Z — fix round wave 1

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-11-04 | WS-11 | C-ARCH 1.4.0 §1.4 (local-only components) and §3 (local composition diagram) | Editorial. Replace `gateway-stub` (pass-through / record / replay / scripted) with "`replicant-gateway-shim` with local upstream backends `passthrough`/`record`/`replay`/`script` plus `gateway-state` Valkey" (C-ARCH already lists the shim in §1.1; XD-03-16). Replace the webhook forwarder text ("`gh webhook forward` or smee; WS-07 decides") with "Cloudflare quick tunnel (`cloudflared`) behind `webhook-ingress` (WS-07 ADR-07-004)". Add local `run-grants` (core run-grant JWKS and `/grants/revoked`, C-RUNNER 2.4.1), `ops-worker` (`replicant-ops`) and `retention-sweeper` (`replicant-retention`) to the §3 diagram, and draw CLI → `replicant-agentd` → shim for sandbox model traffic | The diagram contradicts C-ARCH's own §1.1 (shim, agentd), WS-03 gateway.md §8, and WS-07 ADR-07-004; R1-255/R1-256 | Open |

### Orchestrator adjudication: WS-03 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-03-05 C-STATE row: per-run gateway policy owned by the workflow | **Accepted** | |
| CCR-03-06 `agent_prompt`, `gateway_usage_log`, `run-plan.json` as `run_inputs` | **Accepted** | `gateway_usage_log` is the durable replay store for R1-077 |
| CCR-03-07 shim state on ElastiCache (Valkey) with IAM auth | **Accepted** | Managed service, consistent with the AWS-native posture |
| CCR-03-08 C-RUNNER token-refresh wording erratum | **Accepted** | |
| CCR-03-09 headers `X-Replicant-Served-Model`, `X-Replicant-Purpose`, `X-Replicant-Pipeline-Id` | **Accepted** | Unblocks WS-06 judge-drift check J1 |

### Orchestrator adjudication: WS-05 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-05-04 gate decision `rerun_qa` (V5 extension) | **Accepted** | At most 2 per gate |
| CCR-05-05 QA runs produce `secret_scan_report` | **Accepted** | |

XD-05-20: **Accepted.** Log-wrapped QA services use `replicant/svc-*` images with the wrapper baked in. Unwrapped services use `mirror/`. Both prefixes pass the VAP (WS-09 regex already admits both).

### Orchestrator adjudication: WS-02 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-02-06 `replicant-eval` priority class (value 50) | **Accepted** | Uses the corrected value 50 |
| CCR-02-07 session policy: GetObject on own manifest; Deny `agentrun_record/*`, `inbox/*` | **Accepted** | |
| CCR-02-08 C-IDS env vars, paths, pod labels | **Accepted** | Merge with CCR-09-11 namespace labels in one C-IDS §8 table |
| CCR-02-09 heartbeat, revocation feed, `:revoke`, `inbox/_control.json`, `exec-revocations` key | **Accepted** | |
| CCR-02-10 exact `agentrun-controller` role | **Accepted, merged with CCR-09-08** | `s3:GetObject` authorizes HeadObject. Add the `_control.json` write and `PutObjectTagging` |
| CCR-02-11 OTLP collector in C-ARCH invariant 2 | **Accepted** | |

### Orchestrator adjudication: WS-06 and WS-11 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-06-07 `harness_task`, `pipeline_id`; eval toolchain definitions outside the bundle for `role: eval`; workflow type `ComponentSuite` | **Accepted** | Scope: `role: eval` in tenant `replicant-eval` only. Admission must refuse the out-of-bundle exception for any other role or tenant |
| CCR-06-11 five sign-off points | **Accepted** | |
| CCR-06-12 C-DEF slot rows: shadow and live-tenant pins; `eval_only` refusal | **Accepted** | |
| CCR-06-13 `bundle_rolled_back`, `override_release_block`, `GateView.release_block`, migration `consent` | **Accepted** | `model_retiring` stays consent-free (R2-044) |
| CCR-06-14 `release_evidence_packet`, `judge_anchor_item` | **Accepted** | |
| CCR-06-15 eval-worker reads `jobs_read`, `intake`, `approvals`, `cost` | **Accepted with constraint** | Cross-tenant reads only under the `system:all-tenants` scope, limited to IDs, status, metrics and digests (no free-text columns). Every read is audited. WS-10 implements this as views |
| CCR-06-16 valid example in C-DEF §4.3 | **Accepted** | |
| CCR-11-04 C-ARCH local topology (shim backends, Cloudflare tunnel, `run-grants`, `ops-worker`, `retention-sweeper`, CLI → agentd → shim) | **Accepted** | Editorial |

WS-11 judgement call (kind applies WS-09 VAPs with only the image-registry rule rewritten; unmodified VAPs are tested in
envtest): **accepted**.

## WS-10 — 2026-10-24T19:40Z (fix round wave 1)

| ID | Requester | Contract / section | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-10-07 | WS-10 | C-ART §1, §3 (bucket set per environment) | Add: "Each of the seven buckets has one DR replica `<org>-replicant-<env>-<class>-us-west-2` (CRR + RTC; same keys, same prefixes, KMS multi-Region replica keys). Services accept a replica name **only** when deployed with the `dr-us-west-2` values set during a region failover (recovery.md §7.3); otherwise the 'reject any bucket outside the environment's seven' rule stands. Deleting principals (offboarding, leak purge) act on the primary and its replica." | R1-249: the DR bucket names break C-ART's "only the environment's seven buckets" rule as written, but offboarding and leak purge must delete in both. spec/10-data-platform/recovery.md §7.3, object-storage.md §7 | proposed |
| CCR-10-08 | WS-10 | C-ART §2 `session` bucket writers; C-STATE (session row) | Add replicant-worker as a writer of `session` key `tenants/<t>/jobs/<j>/runs/<run_id>/inputs/*` only, by server-side `CopyObject` from the same job's `cli_session_state/` (`x-amz-tagging-directive: REPLACE`). No other `session` write by replicant-worker | R1-236: resume and takeover stage the prior CLI session into the new run's inputs. A server-side copy keeps raw session state inside the restricted bucket; the alternative (copy through `artifacts`) would move it into a wider-read bucket. spec/10-data-platform/object-storage.md §4.4, config/iam/replicant-worker.s3.json | proposed |
| CCR-10-09 | WS-10 (WS-08 owns `audit.emit`) | C-PG 1.2.0 rule 3 | Append: "Restore exception: `audit.emit` has a restore form, callable only by holders of `r_audit_restore` (held by `replicant_migrator` alone) while an `ops.restore_events` row is open, which accepts the original `audit_event_id` and `occurred_at` and inserts with `ON CONFLICT (audit_event_id) DO NOTHING`. It is the only way lost audit events are re-imported after a database restore; no role inserts into `audit.*` directly." | R1-237: the restore repair imported NDJSON into `audit.events` directly as `audit_owner`, bypassing `audit.emit`. Replaying through `emit` keeps rule 3 intact and makes replay idempotent. spec/10-data-platform/recovery.md §4.2 step 2; XD-FIX-R1-237 | proposed |
| CCR-10-10 | WS-10 | C-ART §1 (bucket list, "outside the seven" like `obs-*`) | Add bucket `<org>-replicant-<env>-legal`: Object Lock **governance**, no default retention, legal holds only, platform `legal` KMS key, written and read only by `replicant-<env>-legal-hold` (assumed by the legal team through WS-09's approval flow). Litigation copies of purgeable content (`artifacts`, `transcripts`, `session` prefixes of named jobs) go here, **never** into `audit` | ADR-WS10-009: `audit` is COMPLIANCE-locked and content-free. Holding purgeable content there would make it unpurgeable for 2–7 years; the tenant buckets have no Object Lock (C-ART 1.9.0), so they cannot hold in place. `replicant-leak-purge` may delete there only when no legal hold stands, else it escalates (`blocked_by_legal_hold`) | proposed |
| CCR-10-11 | WS-10 | C-ART 1.9.0 rule 5 ("the only deletions before lifecycle expiry") | Replace the "only" list with exactly four paths: (1) leak purge (rule 4a, `replicant-leak-purge`; `artifacts`, `transcripts`, `session` + DR replicas + legal bucket); (2) admin memory purge (rule 5, memory-service; its injection and snapshot-export keys only); (3) the session-state sweep (`replicant-retention`, `retention-sweeper`; `session` run prefixes of terminal jobs after `sessionStateGraceDays`, plus `artifacts` `takeover/` per job37d); (4) tenant offboarding (`tenant-offboarding`; every tenant prefix in every bucket except `audit`, `registry`). Each writes a deletion-ledger entry first. `audit` and `registry` deny every delete | R1-252: offboarding and the session sweep already delete before expiry, so the contract's "only" list was wrong. The bucket policies (`DeletersPerBucket` in tools/gen_s3_config.py) implement exactly these four | proposed |

## WS-08 — 2026-10-24T17:30Z (fix round wave 1)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-08-06 | WS-08 | C-ARCH 1.2.0 (deployments, roles); C-PG (logins, rule 3); C-MSG §1 (Temporal identities); C-ART §2 (`audit` bucket writers) | (a) C-ARCH: add Deployment `audit-exporter` (core, `replicant-system`; IAM role `replicant-<env>-audit-exporter`), Deployment `ops-worker` (core, `replicant-workers`, polls `replicant-ops`; service account `leak-purge-worker`, IAM role `replicant-<env>-leak-purge`), CronJob `langfuse-retention-sweeper` (core, `langfuse`; no AWS role), Deployment `alloy-cloudwatch` (core, `observability`; IAM role with `cloudwatch:GetMetricData`, `ListMetrics`, `tag:GetResources`). (b) C-PG: logins `svc_audit_exporter` (member of `audit_reader`, scope `org:*`, plus `EXECUTE audit.record_partition_export`, the second and only other write path into `audit` besides `audit.emit`) and `svc_ops_worker` (`EXECUTE audit.emit` only). (c) C-MSG §1: Temporal identity `audit-exporter`, read-only (`ListWorkflowExecutions`, `GetWorkflowExecutionHistory`); identity `ops-worker` (worker on `replicant-ops`). (d) C-ART §2 `audit` writers: Okta System Log exports are written by the Firehose delivery role `replicant-<env>-okta-log-firehose`, not by the audit exporter | R1-191, R1-192, R1-194, R1-204; contracts name the "WS-08 audit exporter" and `replicant-ops` but no deployment, login or Temporal identity | Open |
| CCR-08-07 | WS-08 | C-IDS 1.11.0 §11 (Prometheus labels) | Allow `status` (C-IDS §6 job status enum), `terminal` (`true`\|`false`), `revision` (schema revision, bounded by migrations, ≤ 1 per 2 weeks), `forbidden_label` (name of a dropped identifier key: `job_id`\|`run_id`\|`principal_id`) | R1-197: these keys are used by `replicant_jobs_in_status`, `replicant_runs_terminal_total`, `replicant_db_schema_revision` and the forbidden-label safety check; all bounded | Open |

WS-08 correction 2026-10-24T17:35Z to CCR-08-07 (rationale wording only): the series are `replicant_jobs_in_status{status}`, `replicant_controller_agentruns{phase,terminal}`, `replicant_db_schema_revision_info{revision}` (Alembic revision ID of a live deployment, about 10 values at most) and the `forbidden_label` marker; the requested change is unchanged.

### Orchestrator adjudication: WS-08 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-08-06 workloads `audit-exporter`, `ops-worker`, `alloy-cloudwatch`, Langfuse sweeper CronJob; logins `svc_audit_exporter`, `svc_ops_worker`; Temporal identities; Firehose role | **Accepted** | `record_partition_export` is the only second write path into `audit`, through SECURITY DEFINER |
| CCR-08-07 C-IDS §11 labels `status`, `terminal`, `revision`, `forbidden_label` | **Accepted** | |

R2-009 decision: one Langfuse org and project per environment, operator-only access. Restricted tenants send no spans to Langfuse.

WS-10 — 2026-10-24T19:55Z erratum to CCR-10-11 item (3): the session-state sweep deletes **only** `session` keys `tenants/<t>/jobs/<j>/runs/*` (all versions) of terminal jobs after `sessionStateGraceDays`. It deletes nothing in `artifacts`; the "plus `artifacts` `takeover/`" clause is withdrawn (the `retention-sweeper` role and the `artifacts` bucket policy grant no such delete).

### Orchestrator adjudication: WS-10 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-10-07 DR bucket mapping | **Accepted** | |
| CCR-10-08 replicant-worker writes session `inputs/` | **Accepted** | |
| CCR-10-09 restore form `audit.emit_restored` (C-PG rule 3) | **Accepted** | Only while an `ops.restore_events` row is open. `ON CONFLICT DO NOTHING` |
| CCR-10-10 legal bucket | **Accepted** | |
| CCR-10-11 pre-expiry deletion list (C-ART rule 5), with erratum | **Accepted as corrected** | |

R1-240 ownership: **WS-09 owns keys; WS-02 owns subnets and firewall.**

Audit bucket Object Lock: **COMPLIANCE, 730 days** (after a 30-day staging trial at 1 day). This is irreversible per object. It is
acceptable only because C-ART 1.9.0 keeps all content and free text out of `audit`. Flagged to the user in the final report.

### WS-07 wave-1 fix round (2026-10-24)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-07-06 | WS-07 (with WS-01) | C-MSG 1.6.0 `PrincipalChangedSignal` | Add `decision_identity_contested` to `change`; add optional `decision_id` (`$defs/decision_id`), required when `change = decision_identity_contested` and forbidden otherwise | R1 §5.1 must-answer reopen path: an approver removed from the group whose approval was accepted before the removal propagated. With tenant setting `on_contested_decision: reopen_if_unpublished` the workflow withdraws that approval and reopens the gate if no publish activity has started for it (identity.md §10 outcome 5, XD-FIX-R1-173). Additive | Proposed |
| CCR-07-07 | WS-07 (with WS-01) | C-MSG 1.6.0 `GateView`, `JobSnapshot.steering[]` | `GateView.decision_log`: array (maxItems 50, oldest dropped first) of `{decision_id, outcome ∈ accepted\|superseded\|denied\|recorded, decision (winning_decision enum plus rerun_qa), principal_id, channel ∈ ui\|cli\|slack\|github\|gitea\|jira, delegate_of?, denial_reason? (GateDecideResult.denial_reason enum), text_key?, text_sha256?, decided_at, presented_digest}`; `text_key` is the C-ART `approval_text` key, never the text. `steering[]` items gain optional `actor` (`$defs/principal_id`) | The read model (approvals.gate_decisions, the job timeline's "superseded" rows in the Slack-vs-GitHub must-answer, the approver history) must come from Temporal only (R1-172); today superseded and denied decisions exist only in Update results and audit. Steering rows need the sender. Additive; the projector tolerates absence | Proposed |
| CCR-07-08 | WS-07 | C-IDS 1.12.0 §3 (IDs) and the `dedupe_key` example | Register Temporal Schedule ID `sch_<ulid>` (prefix `sch_`, Crockford ULID body) for tenant maintenance schedules; the human name moves to a separate `display_name`. Change the `dedupe_key` example `schedule:sch_deps-weekly:…` to `schedule:sch_01m5q7k2x9c4v8b6n3j1h0g5fd:2026-10-26T06:00:00Z` | R1-181: a free-text name in an ID collides across tenants and renames break dedupe. Additive for the prefix; example text only otherwise | Proposed |
| CCR-07-09 | WS-07 | C-JOBREQ 1.3.0 `source.github.app_registration` | Add `staging` to the enum `[prod, dev]` | R1-162: the staging GitHub App registration (github-app.md §1) currently has to report `prod`. Additive | Proposed |
| CCR-07-10 | WS-07 (with WS-01, WS-02, WS-03) | C-MSG 1.6.0 `JobSnapshot` | Add optional `waiting`: `{reason ∈ tenant_run_quota\|cluster_capacity\|tenant_budget_exhausted\|platform_budget_exhausted, since (timestamp), attempt (int ≥ 1), quota_used? (int), quota_limit? (int), queue_position? (int ≥ 1), resets_at? (timestamp, budget reasons), contact_group? (string ≤ 128)}`; absent when the job is not waiting. The workflow sets it from the typed errors of `runner.start_run` and the gateway budget check and clears it when the run starts | R2-020: a job waiting for capacity or budget shows a status the workflow cannot produce and an ETA with no source. Activity heartbeat details are invisible to workflow code and to the projector, so the state must be in the snapshot. The projector maps it to `jobs_read.jobs.sub_status` (`waiting_capacity_tenant`, `waiting_capacity_cluster`, `tenant_budget_exhausted`) and excludes budget waits from `needs_human`. Additive (XD-FIX-R2-020) | Proposed |
| CCR-07-11 | WS-07 (with WS-09, WS-10) | C-PG 1.3.0 schema table, row `github_app` | Readers: `control-plane-api`, **`github-token-broker` (column-limited `SELECT (provider, host, repo_id, installation_id, tenant_id, archived, removed_at, indexing_enabled)` on `github_app.repos`)** | CCR-09-06 has the broker refuse indexer tokens for repos that are not opted in (R1-186); the broker must read the opt-in flag without calling control-plane-api in the token path. Login `svc_github_token_broker`, granted in WS-07 github_app.sql | Proposed |
| CCR-07-12 | WS-07 (with WS-01) | C-MSG 1.6.0 (Updates catalogue) and C-IDS 1.12.0 (message names) | Add Update `ops.retry_node` on `ReplicantJob`: request `{schema: "replicant.msg.retry-node/v1", node_key (graph node key), reason (≤ 2000), actor (principal_id), decision_id-style idempotency key `opr_<ulid>`}`; result `{accepted: bool, new_ordinal?: int, denial_reason?: not_failed\|node_unknown\|job_terminal\|not_allowed}`. Validator: the node's latest attempt is failed or stuck, the job is not terminal, actor holds `release:operate` (checked by control-plane-api before sending). Handler re-runs the node with activity ordinal + 1, so activity IDs never collide with earlier results | R2-048: Temporal UI reset is denied in the Authorizer because deterministic activity IDs would replay earlier results; operators need a supported way to retry a node (`POST /v1/admin/ops/jobs/{job_id}:retry-node`, architecture.md §6). Additive | Proposed |

### Orchestrator resolution: WS-00 wave-1 conflicts (2026-10-24)

1. **Legal bucket (CCR-10-10, CCR-10-11 item 1): option (b), accepted.** My earlier "holds only on `audit`" note
   was an incomplete restatement of WS-10's design, which had already pointed litigation copies of purgeable
   content to a separate legal bucket. Legal holds are allowed in exactly two places:
   - `audit`, which is never purged;
   - `<org>-replicant-<env>-legal`, in governance mode, which holds litigation copies.

   Leak purge covers the legal bucket. A hold that blocks a purge records `blocked_by_legal_hold`, escalates through
   the `leak_purge` gate, and the secret is rotated regardless. Reword rule 4a accordingly.
2. **Execution revocation feed (CCR-02-09): option (b).** The feed moves to the `ops` bucket (CCR-09-10), prefix
   `exec-revocations/<env>/`, with 90-day lifecycle expiry. runner-api gets a prefix-scoped write there. The `audit`
   bucket gains no execution-account writer.

### Orchestrator adjudication: WS-07 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-07-06 `decision_identity_contested`, plus `decision_id` on principal-changed | **Accepted** | |
| CCR-07-07 `GateView.decision_log`, `steering[].actor` | **Accepted** | |
| CCR-07-08 `sch_<ulid>` schedule IDs | **Accepted** | |
| CCR-07-09 `staging` GitHub App registration | **Accepted** | |
| CCR-07-10 `JobSnapshot.waiting` | **Accepted** | Unblocks R2-020 |
| CCR-07-11 token broker as a column-limited reader of `github_app` tables | **Accepted** | Column-limited grant. No access to other identity tables |
| CCR-07-12 Update `ops.retry_node` replaces Temporal reset | **Accepted** | Reset stays operator break-glass only |

Orchestrator screenshot review (approvals 1440 light, job list 1024 dark, rollback dialog 1440 light): **passes the bar**.
No banned patterns. The hierarchy is clear, and the agent-written framing is visible.

Polish for wave 2:
- The approvals list row for `budget_extension` wraps the team name across three lines at 1440 px. Give the meta line a
  single-line truncation rule.

## WS-01 — 2026-10-24T18:00Z (fix round wave 1)

| ID | Filed by | Contract | Change | Why | Status |
|---|---|---|---|---|---|
| CCR-01-06 | WS-01 (with WS-07) | C-MSG 1.6.0 `JobSnapshot.waiting` (CCR-07-10, accepted); C-IDS 1.12.0 §6 `paused` | Extend `waiting.reason` with `dependency_outage`, `operator_hold` and `bundle_revoked`. Add optional `waiting.dependency` (enum `ai-gateway`, `repo-mirror`, `exec-node-pool`, `internal_service`) and `waiting.next_probe_at` (timestamp). C-IDS §6 `paused` meaning becomes "Paused by `job.pause`, or held by Replicant: dependency outage, operator kill switch (`waiting.reason` says which)". `bundle_revoked` holds use status `waiting_human` (already C-MSG) | R2-010, R2-012, R2-016: the workflow now holds instead of failing, and the UI must say why without inventing a status (R2-020). Until accepted, the reference emits status `paused` plus timeline `job.blocked_on_dependency` / `job.held` and leaves `waiting` unset. Additive | Proposed |
| CCR-01-07 | WS-01 (with WS-07) | C-MSG 1.6.0 §2.1 V10 and `GateDecideRequest.decided_at` | V10 becomes: reject `InvalidPayload` if `decided_at > workflow.now() + 60 s` or `decided_at < gate.opened_at − 60 s`. While `workflow.now() < expires_at`, accept. After `expires_at`, accept only if `decided_at < expires_at`, `workflow.now() − decided_at ≤ 6 h`, and the gate's late-expiry grace window (opened when the expiry timer fires > 60 s late; 15 min) has not ended; otherwise `GateClosed`. A held gate (pause `at_next_gate`) does not expire while held. `decided_at` description changes from "Channel-observed time. Informational" to "control-plane-api's receipt time of the decision, unchanged across re-submissions; used by V10" | R2-014: a decision made before expiry was lost when an outage delayed delivery. R2-023: held gates expired unnoticed. Measured: scenarios `late_decision`, `held_gate` | Proposed |
| CCR-01-08 | WS-01 | C-MSG 1.6.0 §1 (Temporal client credentials) | Add rows: `retention-sweeper` (core): poll `replicant-retention`; `ListWorkflowExecutions` (due-job scans, XD-10-05). `replicant-replay-gate` (core, release pipeline Job): read-only `ListWorkflowExecutions`, `GetWorkflowExecutionHistory`. `replicant-projector`: state its read APIs explicitly (`ListWorkflowExecutions`, `QueryWorkflow`, `DescribeWorkflowExecution`). Temporal UI row: `reset` is break-glass only (orchestrator decision on CCR-07-12). The `audit-exporter` and `ops-worker` rows are CCR-08-06's | R1-021, R1-015: identities that call Temporal but are missing from §1, so an Authorizer written from C-MSG denies them | Proposed |
| CCR-01-09 | WS-01 | C-STATE 1.8.0 §2.1 (projection drain) | Continue-As-New and close wait **without** the 10-minute cap for projection events the projector audits (`gate.*`, `bundle.*`, `job.terminal`) and for the approval-record and gate-resolution activities. The 10-minute cap stays for snapshot-only events, which the reconciler repairs | R2-013: a Postgres outage longer than 10 minutes during Continue-As-New lost `gate.decided` audit rows (the reconciler restores snapshots, not audit) | Proposed |
| CCR-01-10 | WS-01 (with WS-09) | C-MSG 1.6.0 §1.1 (run-notifier security) | Replace "a per-identity rate limit at the Temporal frontend (WS-01 sets `frontend.namespaceRPS` overrides)" with "a per-identity token bucket in the custom Authorizer (run-notifier 50 Signals/s, burst 200; WS-09), plus the per-namespace `frontend.namespaceRPS`". Temporal's dynamic config has no per-identity limit [verified: temporalio/temporal v1.32.1 `common/dynamicconfig/constants.go`, 2026-10-24] | R1-022: the contract names a control that does not exist | Proposed |
| CCR-01-11 | WS-01 (with WS-07) | C-ARCH 1.4.0 §1.1 (`temporal-codec-server` "Python or Go") | Fix the language as **Python**. It reuses `TenantContextCodec` and audits each decode through control-plane-api `POST /internal/v1/audit/events` (event type `temporal.payload_decoded`). It has no database login, so D-23 holds | R1-287: the decode audit had no path (no C-PG login, and Go services never touch the DB) | Proposed |

### Orchestrator adjudication: WS-01 wave-1 batch (2026-10-24)

| CCR | Decision | Notes |
|---|---|---|
| CCR-01-06 more `JobSnapshot.waiting` reasons; wider `paused` meaning | **Accepted** | Merge into the CCR-07-10 field |
| CCR-01-07 V10 rule, `decided_at` | **Accepted** | 15-minute grace only when the expiry timer fires late |
| CCR-01-08 identities `retention-sweeper`, `replicant-replay-gate`; projector read APIs; Temporal UI reset as break-glass | **Accepted** | Consistent with CCR-07-12 |
| CCR-01-09 drain audit-bearing projections with no 10-minute cap | **Accepted** | |
| CCR-01-10 per-identity rate limit lives in the Authorizer | **Accepted** | |
| CCR-01-11 codec server is Python | **Accepted** | Consistent with D-23 |

Dispute on XD-FIX-R1-267 (`ops-worker` ownership): **WS-08 owns `ops-worker`** (CCR-08-06). WS-01 supplies
only workflow and activity code hosted there, where WS-08 requests it.

Maximum job lifetime: **60 days** (it exceeds the 30-day gate wait). Recorded as OQ-01-06 for user confirmation.

## WS-09 — 2026-10-24 (fix round wave 2)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-09-12 | WS-09 (for WS-07) | C-ART 1.10.1 §1 "Operations bucket" (exactly two writers) | Add a third, break-glass writer: role `replicant-<env>-killswitch-operator`, `s3:PutObject` on `kill-switches/v1/state.json` only, compare-and-swap required (`If-Match`/`If-None-Match`, enforced in the bucket policy), assumed only through the access broker with a ticket and second approver, every use audited | WS-07's XD-FIX-R2-016-WS07 needs the break-glass CLI to engage a kill switch when control-plane-api is down; the frozen text admits only control-plane-api for that key, so WS-09 left the role out of the bucket and key policies until this is accepted | Proposed |

### Orchestrator rulings: WS-09 wave 2 (2026-10-24)

| Item | Ruling |
|---|---|
| CCR-09-12 `replicant-<env>-killswitch-operator` as a break-glass writer of `ops/kill-switches/*` | **Accepted.** Needed for WS-07's break-glass CLI path when control-plane-api is down. Every write emits audit, and a mirror-drift alert fires. C-ART's `ops` writer list becomes: control-plane-api, runner-api (`exec-revocations/`), killswitch-operator (break-glass, `kill-switches/` only) |
| Legal-bucket writer conflict | **CCR-ORCH-01 (orchestrator-filed), accepted.** C-ART: `replicant-leak-purge` may Put the superseding object and DeleteObjectVersion in the legal bucket, but only for objects with no active legal hold. Holds are placed and removed only by `replicant-<env>-legal-hold`. This implements the earlier ruling that leak purge covers the legal bucket |
| R2-048 Reset | **Confirmed:** operator break-glass only (CCR-07-12), not denied to everyone |
| R2-038 virtio-fs fallback residual risk | **Signed off by the orchestrator** with WS-09's conditions: residual 12, Kata ≥ 4.3.1, sandboxed virtiofsd, Tetragon, restricted tenants excluded, two replicant-security approvers, 90-day expiry. Reported to the user as an accepted risk |

## WS-02 — 2026-10-24T21:30Z (fix round wave 2)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-02-12 | WS-02 (with WS-01, WS-03) | C-RUNNER 2.5.1 (`sandboxHeartbeat` request body; `Problem`), C-IDS 1.12.0 §8 (annotation) | (a) Optional request body `SandboxHeartbeatRequest` = `{disk?: {state: "ok"\|"warn"\|"exceeded", used_bytes: int ≥ 0, limit_bytes: int ≥ 1, peak_bytes: int ≥ 0}}` (additionalProperties false; a body-less heartbeat stays valid). runner-api copies it to the run Lease annotation `replicant.example.com/disk` = `<state>:<used_bytes>/<limit_bytes>` (register in C-IDS §8). (b) `Problem` gains optional integer members `quota_used` and `quota_limit` (≥ 0), set only with code `tenant_quota_exceeded` | (a) XD-FIX-R1-210 removes `nodes/proxy`, so the controller can no longer read kubelet volume stats; the watchdog's report is the only per-run disk signal and reaches the controller through the heartbeat and the Lease it already watches (controller.md §11). (b) XD-FIX-R2-020 / CCR-07-10 need `quota_used`/`quota_limit` for `JobSnapshot.waiting`; `retry_after_seconds` already exists. Additive | Proposed |

## WS-10 — 2026-10-24T22:30Z (fix round wave 2)

| ID | Requester | Contract / section | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-10-12 | WS-10 (with WS-01, WS-09) | C-MSG 1.6.0 §1 (Temporal client credentials) | Add row: `replicant-db-migrate` (core, the migrate PreSync Job): `CountWorkflowExecutions` on namespace `replicant-<env>` only, read-only, no other API | XD-FIX-R1-028: the contract gate must prove no workflow pinned to a pre-expand `replicant-worker` version is still running (`CountWorkflowExecutions("TemporalWorkerDeploymentVersion IN (…) AND ExecutionStatus = 'Running'") = 0`). Without a C-MSG row, an Authorizer written from C-MSG denies the migrate Job. spec/10-data-platform/schema-management.md §7.4 | proposed |

## WS-01 — 2026-10-24T21:00Z (fix round wave 2)

| ID | Filed by | Contract | Change | Why | Status |
|---|---|---|---|---|---|
| CCR-01-12 | WS-01 (with WS-09) | C-MSG 1.7.x §1 (worker row) | Add `CountWorkflowExecutions` (read-only, own namespace) to the `replicant-worker` identity's permissions | XD-FIX-R1-195 needs `replicant_worker_pinned_open_workflows{deployment_version}` from the worker. The worker row lists only polling plus Start/Signal from activities, so an Authorizer written from C-MSG denies the count (XD-W2-01-01) | Proposed |

## WS-03 — 2026-10-24 (fix round wave 2)

| ID | Filed by | Contract | Change | Why | Status |
|---|---|---|---|---|---|
| CCR-03-10 | WS-03 (with WS-06) | C-IDS 1.13.x §7 (headers) | (a) `X-Replicant-Role` (service caller → shim) also takes `eval-judge`, sent only by eval-worker with `X-Replicant-Purpose: eval_judge`. (b) `X-Replicant-Budget-Exceeded` values become `run`, `job`, `tenant_period`, `tenant_embeddings_day`, `pipeline`, `account`, `platform` (the first three are today's; the shim already emits `tenant_embeddings_day`/`pipeline` for GW-13 and now `account` for budget accounts and `platform` for the platform ceiling). (c) `X-Replicant-Served-Model` description: informational; consumers compare the response body's `model` field, which the shim passes through unchanged | XD-FIX-R2-063, XD-FIX-R2-061, XD-FIX-R2-055, XD-FIX-R1-150 (WS-06 drops the header) | Proposed |

## WS-00 — 2026-10-24 (wave 3, filed and applied on orchestrator instruction)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-00-01 | WS-00 | C-JOBREQ 1.4.1 → 1.4.2; C-IDS 1.13.4 (`MaintenanceShard`); C-MSG 1.7.4 (`batch.child_progress` accepted by `MaintenanceShard`); C-ARCH 1.5.4 §4.1 | `target_selector.max_repos` maximum 1,500 → 15,000; `MaintenanceShard` fan-out above 2,000 repositories; the scale-limits table | R1V-006 / R2-046: the frozen limits had no 10x path. Shape in XD-W3-R1V-006 | **Applied by WS-00** (orchestrator instruction to decide and describe; please ratify) |

## WS-10 — 2026-10-25T01:10Z (fix round wave 3)

| ID | Requester | Contract / section | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-10-13 | WS-10 | C-ART 1.10.2 rule 5 item 3 (session-state sweep) | Replace "**only** `session` keys `tenants/<t>/jobs/<j>/runs/*` (all versions) of terminal jobs after `sessionStateGraceDays`" with "**only** `session` objects tagged `replicant-retention=job37d` under `tenants/<t>/jobs/<j>/runs/*` (all versions) of terminal jobs after `sessionStateGraceDays`. Takeover workspace archives (tagged `r30d`) are never swept; lifecycle expires them" | R1V-066: as written, rule 5.3 lets the sweeper delete takeover archives at terminal + ≤ 7 days, contradicting the `workspace_archive` row ("job37d (takeover: r30d)"), the 30-day takeover promise (XD-FIX-R2-026) and capacity-and-cost. WS-10's sweeper already skips `r30d` objects (object-storage.md §3.3) | proposed |

### Orchestrator adjudication: WS-00 wave 3 (2026-10-24)

- **CCR-00-01 is ratified**: `max_repos` 15,000, and a `MaintenanceShard` child per 2,000 repos (half of WS-01's measured
  history bound). WS-01, WS-02, WS-04 and WS-10 conform per XD-W3-R1V-006.
- **R1V-050 is confirmed**: audit-exporter holds `EXECUTE audit.emit` and a column-limited `approvals` read.
- **R1V-066 is confirmed**: the session-state sweep skips `workspace_archive/`, which keeps 30-day retention.
- **R1V-046 is confirmed, with a correction to my XD-W2-08-05 ruling text.** The `approval_text` writer is replicant-worker
  (C-STATE), not control-plane-api. The workflow activity computes `text_sha256` when it writes the object and carries
  it in `GateView.decision_log[].text_sha256` (C-MSG). The projector stores it in `approvals.gate_decisions.text_sha256`.

## WS-02 — 2026-10-24T23:30Z (fix round wave 3)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-02-13 | WS-02 (with WS-08) | C-IDS 1.13.3 §8 (annotations) | Register RuntimeClass annotation `replicant.example.com/log-transport` with values `stdout` (default when absent) and `otlp`; writer: the `replicant-exec` chart (set only on a RuntimeClass that fails benchmark B-LOG-01); reader: agentrun-controller, which copies it into bootstrap `endpoints.log_transport` | R1V-098: every other `replicant.example.com/` key is registered; this one gates where sandbox logs go (XD-FIX-R2-039). Additive | Proposed |

### Orchestrator adjudication: CCR-02-13 (2026-10-24)

**Accepted, and already applied.** The `replicant.example.com/log-transport` RuntimeClass annotation was registered in
C-IDS 1.13.4 under R1V-098. No further contract change is needed.

### Orchestrator adjudication: CCR-10-13 and R1V-063 (2026-10-24)

- **CCR-10-13: accepted in its corrected form.** C-ART rule 5.3 now says the session-state sweep deletes all session keys
  except `workspace_archive/` keys, which keep 30-day retention.
- **R1V-063: WS-10's decision stands.** S3 Batch Operations has no delete operation (verified). Offboarding deletes run
  directly as `replicant-<env>-tenant-offboarding`. WS-09 removes `replicant-<env>-s3-batch-offboarding` from its deleter
  lists.

## Orchestrator — 2026-10-25 (product rename)

| ID | Requester | Contract | Change | Rationale | Status |
|---|---|---|---|---|---|
| CCR-ORCH-02 | Orchestrator (user decision) | All: C-ARCH 1.5.4, C-STATE 1.10.2, C-IDS 1.13.4, C-RUNNER 2.5.3, C-CRD 2.4.0, C-MSG 1.7.4, C-JOBREQ 1.4.2, C-DEF 2.5.0, C-ART 1.10.4, C-PG 1.4.3 | **Product rename to Replicant.** Every product-named identifier is renamed, with case variants: domain `factory.example.com` → `replicant.example.com` (labels, annotations, CRD group `agentruns.replicant.example.com`, schema `$id` host `schemas.replicant.example.com`; `$id` path majors unchanged); env prefix `FACTORY_` → `REPLICANT_`; headers `X-Factory-*` → `X-Replicant-*`; `factory-*`/`factory_*` → `replicant-*`/`replicant_*` (services, packages, task queues, Temporal namespace `replicant-<env>`, deployment `replicant-worker`, DB `replicant`, roles `svc_replicant_*`/`replicant_migrator`, buckets `<org>-replicant-<env>-*`, Okta groups, hosts `*.replicant.internal`/`*.replicant.localhost`, networks, priority classes); CamelCase `Factory*` → `Replicant*` (workflow type `ReplicantJob`, search attributes, alert names); bot `replicant-bot`, label `replicant`, branches `replicant/<job_id>` and `replicant-takeover/**`; OTel attributes `replicant.*`; derivation seeds `replicant.job.trace.v1:`, `replicant.job.span.v1:`, `replicant.run.span.v1:`, session `uuid5(URL, "replicant:<run_id>")` (the C-IDS test vectors change). The C-RUNNER `ImplementerRunSpec` example carries product names, so its `spec_digest` test vector is now `sha256:a05ea6bc4aea3f59a4b8f7a20dea9badc8e8d1dcbb8f21b696224337bbabde06`; C-DEF digest vectors that cover product-named documents change the same way. Not renamed: C-IDS ID prefixes (`job_`, `run_`, …), third-party identifiers, and the generic phrase "software factory". | The user named the product Replicant. Nothing has shipped, so no compatibility window, aliases or data migration are needed. Every identifier change is breaking for an external consumer, so every contract takes a major bump. | **Accepted, applied 2026-10-25** |

### Orchestrator adjudication: CCR-ORCH-02 (2026-10-25)

**Accepted.** New versions: C-ARCH 2.0.0, C-STATE 2.0.0, C-IDS 2.0.0, C-RUNNER 3.0.0, C-CRD 3.0.0, C-MSG 2.0.0,
C-JOBREQ 2.0.0, C-DEF 3.0.0, C-ART 2.0.0, C-PG 2.0.0. The identifier rename was applied mechanically across all of
`spec/` (a scripted mapping with a reviewed dry run), so spec and the monorepo agree. Citations of the previous current
versions now cite the new ones; historical records (this file, `90-reviews/`) keep the versions they cited at the time.
The monorepo `contracts/` copies are byte-identical to `spec/00-contracts`.
