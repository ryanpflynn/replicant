# Phase 3 Triage

Orchestrator triage of R1 (`R1-consistency.md`) and R2 (`R2-red-team.md`) findings. Dispositions: **fix**
(owner changes the spec), **accept** (risk accepted, rationale given), **reject** (finding is wrong, rationale
given). The first owner listed leads the fix and coordinates the others named.

Both R2 criticals were re-checked by the orchestrator against the source:
- R2-002: C-RUNNER `spec_digest` excludes only `run_grant`. `trace_context` is in RunSpec, so the finding
  is confirmed.
- R2-001: no gate rule covers CI-executed paths. The finding is confirmed.

## Orchestrator decisions on R2

| Item | Decision |
|---|---|
| R2-005 leak purge | **Adopt R2's third option.** History exports are split: a payload-free skeleton with payload digests in the Object-Lock audit bucket (never purged), and the full encrypted history in the tenant-keyed `artifacts` bucket (purgeable normally). Remove `s3:BypassGovernanceRetention` from every role; the audit bucket may then move to compliance mode (WS-10 decides, and records the legal-hold interaction). Crypto-shred is rejected: it is infeasible under the per-tenant data-key codec, and per-job KMS keys cost about $110k/month |
| R2-001 CI pre-review execution | **Fix, layered.** (1) A non-removable `ci_executed_paths` risk class at `critical` on every publish, repair push, and takeover push. (2) A takeover push is a gated action, or it pushes to a `replicant-takeover/**` prefix that the tenant onboarding checklist requires CI to exclude. (3) Changes to test files raise risk to at least `medium`. (4) Add T-PI-13 to the threat model. (5) WS-07 tenant onboarding documents the recommended org ruleset/environment protection: secrets-bearing workflows must not run for `replicant/**` branches without approval. Fork-based publishing is the recorded alternative (OQ-ORCH-01 for the user) |
| R2-044 model retirement | **Fix.** Retirement forces migration at the next safe point without requester consent. Consent applies to behaviour changes, not to a model that will stop existing. A hard maximum job lifetime is added (WS-01 proposes the value; it must exceed the 30-day gate wait) |
| R2-054 engine switch for live jobs | **Accept.** Switching the engine is a consented migration, like any bundle change. Documented; no design change |
| R2-047, R2-049, R2-050, R2-064 (low) | **Fix.** Each is small |
| R2-048 (low, mixed) | **Fix.** Each owner takes its own items |

## R2 dispositions

| ID | Sev | Disposition | Owner (lead first) |
|---|---|---|---|
| R2-001 | Critical | fix (see above) | WS-09, WS-07, WS-01 |
| R2-002 | Critical | fix: `spec_digest` also excludes `trace_context` (and any other per-attempt field; WS-00 enumerates them). Contract patch C-RUNNER 2.4.1 | WS-00, WS-02, WS-08 |
| R2-003 | High | fix: GOPROXY to a replicant-controlled mirror with `GOFLAGS=-mod=mod`, GONOSUMDB off, GOPRIVATE routing to repo-mirror, and an allowlist of module path prefixes for all tenants (not only `restricted`) | WS-09, WS-02 |
| R2-004 | High | fix: approval records keep free text in `artifacts`, referenced by digest from the audit record | WS-00, WS-07, WS-08 |
| R2-005 | High | fix (see above) | WS-08, WS-09, WS-00, WS-10 |
| R2-006 | High | fix: WS-05 QA report adds a base-vs-head test inventory (`tests_removed`, `tests_skipped_added`); WS-01 verifier enforces it; WS-07 packet shows it | WS-05, WS-01, WS-07 |
| R2-007 | Medium | fix | WS-07, WS-09 |
| R2-008 | Medium | fix: episodes never auto-activate from model-written text without probation; align the threat model | WS-04, WS-09 |
| R2-009 | Medium | fix: one Langfuse tenancy model (WS-08 decides, WS-09 aligns) | WS-08, WS-09 |
| R2-010 | High | fix: retry exhaustion goes to a `blocked_on_dependency` pause with operator resume and `_finish` compensation; recovery.md corrected | WS-01, WS-10 |
| R2-011 | High | fix: the `gate.decide` handler cannot fail after validation (durable record via local state, side effects in a follow-on activity); gates.md corrected | WS-01, WS-07 |
| R2-012 | High | fix: gateway-outage circuit breaker pauses runs (no re-provision), with jittered resume | WS-01, WS-03 |
| R2-013 | Medium | fix | WS-01, WS-08 |
| R2-014 | Medium | fix: expiry is evaluated against the decision's signed client timestamp within a skew bound | WS-01, WS-07 |
| R2-015 | Medium | fix: handoff v1 failure paths defined (frozen protocol keeps its wire format; behaviour is clarified) | WS-01 |
| R2-016 | High | fix: kill switches at global, tenant, repo, and bundle scope; stored in Postgres **and** pushed to Temporal (Signal/Update fan-out) plus a worker-local config-map fallback that works with Postgres down; SC-48 met | WS-07, WS-06, WS-09 |
| R2-017 | High | fix: post-merge outcome signals (revert detection, linked follow-up bugs, explicit "this was wrong" action) feed memory demotion and promotion metrics | WS-04, WS-06, WS-07, WS-01 |
| R2-018 | High | fix: provenance view showing which memory a comment produced, with contributor-requested correction (admin-applied, per D-25) | WS-04, WS-07 |
| R2-019 | High | fix: "Request changes" text is routed into the repair cycle input | WS-01, WS-04, WS-07 |
| R2-020 | High | fix: statuses come from the C-IDS vocabulary only; ETA shown only with a source; every waiting state has an action | WS-07, WS-01, WS-02, WS-08 |
| R2-021 | High | fix: rollback marks gates on the bad bundle with a banner and blocks approval until migrated or explicitly overridden; dialog defaults to "migrate" during a canary failure | WS-06, WS-07, WS-01 |
| R2-022 | Medium | fix | WS-07, WS-03 |
| R2-023 | Medium | fix: pause suspends the expiry timer and notifies | WS-01, WS-07 |
| R2-024 | Medium | fix: approver queue with sort, filter, and batch actions (two-person rules preserved) | WS-07 |
| R2-025 | Medium | fix | WS-07, WS-01 |
| R2-026 | Medium | fix | WS-07, WS-10 |
| R2-027 | Medium | fix: "re-run QA" action on an inconclusive QA result | WS-05, WS-07, WS-01 |
| R2-028 | Medium | fix: any job viewer can flag memory; flags go to the admin queue (D-25 preserved) | WS-04, WS-07 |
| R2-029 | Medium | fix: re-render the screenshots and verify the critique items | WS-07 |
| R2-030 | Medium | fix: run lease with heartbeat; the controller revokes the grant when the lease expires | WS-02, WS-03, WS-09 |
| R2-031 | Medium | fix: Publisher is idempotent across branch → PR (lookup by head branch) | WS-07 |
| R2-032 | Medium | fix: Slack/GitHub decisions validate against Temporal (Update validator), not the read model | WS-07 |
| R2-033 | Medium | fix: QA checkpoints at stage boundaries; S3 throttling retried at the part level, not by re-running the run | WS-02, WS-05, WS-10 |
| R2-034 | Medium | fix: separate the action accent from the warning hue; tokens updated | WS-07 |
| R2-035 | Medium | fix | WS-07 |
| R2-036 | Medium | fix: add prototype states for failed, waiting-for-capacity, stale-data, and Slack/GitHub messages | WS-07 |
| R2-037 | High | fix: B-NET-01 gates the Kata default; fallback rule 6 exposes repo-mirror via an internal NLB with `toFQDNs` | WS-02 |
| R2-038 | Medium | fix: the virtio-fs fallback raises the T-SBX-03 residual and requires WS-09 re-approval | WS-02, WS-09 |
| R2-039 | Medium | fix: add B-LOG-01, with an OTLP stdout fallback | WS-08, WS-02 |
| R2-040 | Medium | fix: cancel reason delivered via the run inbox control key | WS-02, WS-03 |
| R2-041 | Medium | fix: specify a resolver proxy per ecosystem, or accept the risk per ecosystem with rationale | WS-09, WS-02 |
| R2-042 | Low | fix: fallback lines | WS-09, WS-07 |
| R2-043 | Medium | fix: the run-level turn budget is enforced by agentd (it counts turns across steering), not by `--max-turns` alone | WS-03 |
| R2-045 | High | fix: runtime class is an operator-overridable execution policy outside the bundle digest (security exception path), with re-verification | WS-02, WS-09, WS-06 |
| R2-046 | Medium | fix: each limit gets a 10x note and the change path | WS-00, WS-01, WS-02, WS-04, WS-10 |
| R2-047 | Low | fix | WS-07, WS-04 |
| R2-048 | Low | fix | each named owner |
| R2-049 | Low | fix | WS-07 |
| R2-050 | Low | fix | WS-07 |
| R2-051 | Medium | fix: partitioned or filtered HNSW per tenant, pruning of dead versions, a recall eval, and snapshot compaction | WS-04, WS-10 |
| R2-052 | Medium | fix: curation throughput sizing; on candidate expiry, notify, not silent drop | WS-04, WS-07 |
| R2-053 | Medium | fix: one session-resume rule, owned by WS-03; others cite it | WS-03, WS-04, WS-06 |
| R2-054 | Low | accept (see above) | WS-01 |
| R2-055 | High | fix: tenant quota required, with a platform default and a platform-wide daily ceiling | WS-07, WS-03 |
| R2-056 | High | fix: a budget stop is not "invalid"; re-runs are capped per candidate; eval spend ceiling per month | WS-06, WS-02 |
| R2-057 | High | fix: embeddings route through the gateway with budget and attribution | WS-03, WS-04 |
| R2-058 | Medium | fix: unparsed usage fails closed after N; break-glass keeps a hard ceiling | WS-03 |
| R2-059 | Medium | fix: a per-PR repair job cap | WS-07, WS-01 |
| R2-060 | Medium | fix: `budget_usd_max` is an absolute ceiling | WS-03, WS-06 |
| R2-061 | Medium | fix: shadow spend is charged to the platform eval budget; cap enforced by the gateway | WS-06, WS-03 |
| R2-062 | Medium | fix: batch spend cap | WS-01, WS-03 |
| R2-063 | Medium | fix | WS-06, WS-03 |
| R2-064 | Low | fix | WS-02, WS-03 |

Lens-6 housekeeping (stale tags, smee version, V00-42 citation, `CLAUDE_CODE_RETRY_WATCHDOG`, MB vs MiB, KMS
alert at 30%) goes to each file's owner as **fix**.

## R1 dispositions

**All 288 R1 findings (25 blocker, 156 major, 107 minor): fix**, with the owner R1 names. The first owner
listed leads. R1 found the frozen contracts internally consistent; the defects are at seams, where one side
wrote its half of an interface and the provider never adopted it. Rule for every seam finding: **the contract
wins**. Where the contract is silent, the lead owner decides and records it as an `XD-FIX-<finding id>`
entry in `spec/_coordination/cross-ws-dependencies.md`, and the co-owners conform in wave 2.

### Corrections to the queued items (R1 §2)

| Item | Decision |
|---|---|
| XD-ORCH-03 scope vocabulary | Extended to WS-07, WS-03 and WS-05 as well as WS-06 and WS-08 |
| XD-ORCH-03 default privileges (WS-10) | Fix: WS-10's `ALTER DEFAULT PRIVILEGES` must not grant beyond what the owning DDL grants. Owners' narrowing grants (identity token columns, append-only history) are authoritative. WS-10's validator gains a check for this |
| XD-ORCH-03 `grafana_ro` grants | Added to the queue: each ecosystem owner adds its `grafana_ro` grants, WS-10 verifies |
| XD-ORCH-03 multipart and `PutObjectTagging` | All five WS-09 files, not two |
| XD-ORCH-01 assignment window | WS-06 defines the "expected assignment window". Model retirement gets its own notice kind `model_retiring` (C-MSG), not the worker-retirement kind |
| WS-08 leak-purge item | Superseded by the R2-005 decision: purge targets the full history export in `artifacts`. The `replicant-leak-purge` role (WS-10/WS-09) deletes there without any governance bypass. Two-person approval uses gate kind `leak_purge`, added to C-MSG |
| Third exec role `agentrun-controller` | Contract change: WS-00 aligns the C-ARCH text with WS-02's design (WS-02 states the exact permissions) |
| run-notifier and the tenant codec | **Accepted: a fourth exec role, `run-notifier`, with encrypt-only access** (`kms:GenerateDataKey` under tenant keys, no Decrypt). Signal payloads are encoded and never decoded execution-side. WS-00 adds it to C-ARCH, WS-09 writes the IAM and key policy |

### Unanswered or partial must-answers (R1 §5.1)

Each is assigned to its workstream as a **fix**: the answer must be complete and consistent with the
rest of the spec.

### Syntax failures

These are assigned to the owners named in R1's Syntax table and are **fix**:

- the YAML failure at `github-app.md:532`;
- the operationId and component collisions in the merged OpenAPI documents;
- `migrate-job.yaml` `podFailurePolicy` status;
- 11 Mermaid diagrams that fail to render;
- `memory.sql` line 984.

## Fix-round procedure

- **Wave 0:** WS-00 applies the contract changes accepted above:
  - C-RUNNER digest exclusions;
  - C-ART approval-record free text and the split history export;
  - C-ARCH roles `agentrun-controller` and `run-notifier`;
  - C-MSG `model_retiring` and `leak_purge`.
- **Wave 1:** each workstream fixes every R1 and R2 finding it leads. For seam findings it writes an
  `XD-FIX-<id>` resolution. Any contract need goes in as a CCR.
- **Wave 2:** each workstream applies every `XD-FIX` entry that names it as co-owner. The fix round's
  coordination items (XD-ORCH-01..03, ADR-07-004 tunnel default, WS-09 VAP/tenancy/IAM, CCR-07
  versions) close here.
- **Verification:** R1 re-runs on every changed section, plus its validators.

## Resolution status (2026-10-24)

| Step | Status |
|---|---|
| Wave 0: contract fixes (R2-002, R2-004, R2-005, R2-016, R2-044, roles) | Done |
| Wave 1: all 12 workstreams fixed the findings they lead, writing XD-FIX entries and CCRs | Done. Every CCR was adjudicated; see contract-change-requests.md |
| Wave 2: co-owners applied the XD-FIX, XD-ORCH and XD-W2 entries; disputes were ruled | Done. Rulings are in cross-ws-dependencies.md |
| Final contract versions | C-ARCH 1.5.3, C-STATE 1.10.1, C-IDS 1.13.3, C-RUNNER 2.5.2, C-CRD 2.4.0, C-MSG 1.7.3, C-JOBREQ 1.4.1, C-DEF 2.5.0, C-ART 1.10.2, C-PG 1.4.2 |
| WS-10 integration gate (grants, RLS, partitions, eval reader) | In progress |
| R1 verification re-run (`R1-verification.md`) | In progress |

Notable orchestrator rulings made during the fix round:
- Split history export: no role holds a governance-retention bypass.
- Audit bucket in COMPLIANCE mode for 730 days.
- Legal holds allowed only on `audit` and the legal bucket.
- Revocation feed lives in `ops` and is read only by runner-api. Verifiers call `GET /v1/revocations`.
- Canonical scope rule: services use `system:all-tenants`; humans, and read paths that serve them, use `operator:all-tenants`.
- Partition-level RLS: XD-ORCH-04, found by WS-05.
- `audit-exporter` never decrypts tenant content: XD-W2-08-05 was withdrawn.
- Maximum job lifetime is 60 days, pending user confirmation.
- The R2-038 virtio-fs fallback residual risk is signed off with conditions.
- `fallback_unregistered` is removed, because intake never falls back.

### Update: verification results and wave 3

- WS-10 integration gate (STRICT): **passed**.
  - All 17 owner DDL files apply. Grants OK. 0 tables without RLS or FORCE. 39/39 partitions isolated.
  - Marker roles hold no privileges. The audit-exporter reads are column-limited.
- R1 verification (`R1-verification.md`):
  - Original R1 findings: 226/288 fixed, 62 partial, 0 not fixed.
  - R2 fix findings: 42/65 fixed, 22 partial, 1 not fixed.
  - Remaining issues: 187 (4 blocker, 68 major, 115 minor), plus 138 stale citations.
  - All 160 Mermaid diagrams now render.
- Wave 3 dispatched to all 12 owners to close every R1V row and stale citation. The 4 blockers are in exec-account
  runner-api and run-notifier permissions (WS-09 leads, with WS-02).

## Closure (2026-10-24)

Closed under the user's instruction "blockers only from here; log everything else as known issues and finish."

- **Blockers:** all four R1V blockers are fixed and orchestrator-checked.
- **Integration gate:** the final WS-10 `STRICT=1` gate passed.
- **Wave 3:** every remaining row is reported fixed by its owner, but that is not independently re-verified.
- **Open items:** see `known-issues.md`.
- **Plan and assembly:** the implementation plan, traceability and spec README are in `99-implementation-plan/` and
  `spec/README.md`.
