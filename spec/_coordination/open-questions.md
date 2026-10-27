# Open questions — product decisions needing a human (WS, question, options, recommendation, who decides). Append-only.

## WS-00 — 2026-10-24

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-00-01 | Accept **Challenge: D-07** ([ADR-014](../00-contracts/adr/014-challenge-d07-fsquota-monitoring-is-not-enforcement.md))? `LocalStorageCapacityIsolationFSQuotaMonitoring` only monitors usage and does not enforce a limit | (a) accept the replacement text (enforcing mechanisms per runtime plus watchdog); (b) keep D-07 as written | (a). It is a strict superset of D-07's intent | Orchestrator |
| OQ-00-02 | Separate execution EKS cluster per environment ([ADR-007](../00-contracts/adr/007-separate-execution-cluster.md)) | separate exec cluster; single cluster with isolated node pools | Separate. A sandbox escape then lands outside the cluster holding Temporal, Postgres, and the GitHub key | Orchestrator, with WS-02, WS-09 |
| OQ-00-03 | A job stays open through PR review and repair until merge, close, or expiry ([ADR-009](../00-contracts/adr/009-job-lifetime-spans-pr-review.md)) | one long job; a new job per review round | One long job, with Continue-As-New per repair cycle and a 30-day PR wait expiry | Product (orchestrator) |
| OQ-00-04 | Add `repo-mirror` as a third installation-token holder (read-only), with `github-token-broker` as sole private-key holder ([ADR-011](../00-contracts/adr/011-github-token-broker-and-repo-mirror.md)). This extends WS-07's "only Publisher and webhook/reply service" statement | broker + mirror; sandboxes fetch with scoped read tokens; publisher-side git bundles | Broker + mirror | WS-07, WS-09 |
| OQ-00-05 | Tenant-derived bundles auto-promote when the tenant layer only tightens ([ADR-013](../00-contracts/adr/013-behavioral-vs-operational-config-tenant-derived-bundles.md)). Does that count as "passing the evaluation harness" (D-03)? | mechanical tightening check counts as an eval tier; full eval per tenant change | Mechanical check plus a smoke eval for binding changes | WS-06, orchestrator |
| OQ-00-06 | Live steering requires agent definitions with `steering.live`. Should reviewer runs on Codex use app-server (steerable) instead of `codex exec`? | exec (simpler) / app-server (steerable) | `exec` for reviewer (short runs); Claude stream-json for implementer and repair | WS-03 |
| OQ-00-07 | Temporal hosting: self-hosted (PostgreSQL 16) vs Temporal Cloud ([ADR-003](../00-contracts/adr/003-temporal-self-hosted-vs-cloud.md)) | self-hosted / Cloud | Self-hosted | WS-10 with WS-01 |
| OQ-00-08 | One Temporal namespace per environment (tenancy via search attributes and authorization) | per env / per tenant | Per environment | WS-01, WS-09 |
| OQ-00-09 | Does an internal AI/ML gateway exist that supports per-run credentials (accepting the Replicant run token, or exchanging it), per-run and per-job budgets, attribution headers, and usage export? | existing gateway / replicant-owned thin gateway in front | WS-03 states the minimum interface. If missing, deploy a replicant-owned thin gateway (for example LiteLLM proxy) in front of the enterprise gateway | Platform owner, WS-03 |
| OQ-00-10 | Should the Replicant keep any **raw** (unredacted) transcripts? | never; short-lived restricted copy | Never. Only `cli_session_state` is raw (restricted bucket, job + 7 days) | WS-08, WS-09 |
| OQ-00-11 | Publisher ownership | WS-07 / WS-01 | WS-07 owns `publisher-worker` (GitHub semantics); WS-01 owns graph placement | Orchestrator |

## WS-11 — 2026-10-24T13:51Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-11-01 | Accept **Challenge: D-08** (spec/11-local-dev/adr/004-challenge-d08-minio-locally.md)? MinIO's repo is archived and the community edition is source-only | (a) SeaweedFS default, MinIO-from-source as a profile; (b) MinIO from source as default | (a) | Orchestrator |
| OQ-11-02 | Run Langfuse in the `core` profile because prompts are resolved from it for every bundle (ADR-11-005) | core / observability with a prompt stub | core | Orchestrator, WS-08 |
| OQ-11-03 | Identity link kind for Gitea users (CCR-11-01) | add `gitea` / encode as `github` with host-prefixed id | add `gitea` | Orchestrator |
| OQ-11-04 | Allow a local-only dev seed token in control-plane-api | dev token with startup guard / seed via interactive admin login | dev token, refused unless `REPLICANT_ENV=local`, audited per use | WS-07, WS-09 |
| OQ-11-05 | Source of KVM-capable CI runners for the `k8s-kata` job | EC2 nested-virt (`m8id`/`c8id`) runners via ARC / bare metal | nested-virt via ARC | WS-10 |

## WS-09 — 2026-10-24

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-09-00 (re OQ-11-04) | WS-09 position on the local-only dev seed token `svc_dev-seeder` | runtime `REPLICANT_ENV=local` flag only / build-time exclusion with layered guards / interactive admin login only | **Build-time exclusion is required**: the authenticator ships only in the `dev` image target (CI proves the prod image cannot import it), dev images are signed with a dev-only key that staging/prod admission rejects, startup guard refuses the token outside local, `svc_dev-seeder` exists only in local seed data and identity-service denies it elsewhere, token limited to seed endpoints, Helm has no setting for it, and any non-local use pages security. A runtime flag alone is rejected. Detail: spec/09-security/secrets.md §11 | WS-07 with WS-09 |
| OQ-09-01 | Which tenants are `restricted` (dedicated sandbox isolation groups, opaque slugs, mandatory publish gate)? | none / classification-driven / all | Classification-driven: payments core, identity, security tooling at launch | Security leadership, tenant owners |
| OQ-09-02 | Accept residual risk R-04: a compromised runner-api can vend STS sessions for any tenant's run prefixes (S3 cannot verify run grants) | accept with detection / core-side credential broker | Accept with detection (CloudTrail data events vs active runs) for v1; revisit after first red-team | Orchestrator, WS-02, WS-09 |
| OQ-09-03 | Two-person rule for (a) tenant-scope memory activation and (b) every prod promotion that changes prompts or behavioral policy | single person / two people | Two people for both (threat-model T-MEM-04, T-INS-06; control-review G-09) | WS-04, WS-06, orchestrator |
| OQ-09-04 | Aurora PostgreSQL vs RDS for PostgreSQL for `replicant`, given D-26 "full session audit" for break-glass (DAS exists only on Aurora for PostgreSQL) | Aurora / RDS + pgaudit | Aurora if cost acceptable | WS-10 |
| OQ-09-05 | Live verification of secrets found in code (calling the provider to test validity) | never / opt-in per tenant | Opt-in per tenant, from a core service only | Security leadership |
| OQ-09-06 | Which SOC 2 categories are in scope? | Security / + Confidentiality / + Processing Integrity / + Privacy | Security + Confidentiality | Compliance owner |

## WS-06 — 2026-10-24T14:00Z

**Decision on OQ-00-05 (owned by WS-06):** **Yes.** A tenant-derived bundle whose layer the mechanical
policy-differential check classifies as *tightening* or *neutral* passes harness tier **T0** and auto-promotes
(actor `svc_release-controller`, under the admin-configured promotion policy), provided the closure outside
`policies` is digest-identical to its promoted (or canary) base and the budget-sufficiency check passes (else a
tenant-admin acknowledgment). Justification against D-03: every behavior-determining element is the evaluated
base's by digest; the harness scripts gates, so an eval of a gate-only layer would measure nothing new; lower
caps fail closed. Behavioral layers (including bindings that move repos between definitions) need a targeted T1
and two-person sign-off. See `spec/06-releases-and-evals/adr/001-tightening-only-derived-bundles-auto-promote.md`
and CCR-06-06. **Response to OQ-09-03(b):** accepted; two-person sign-off applies to prompt and behavioral-policy
changes in production (WS-09 G-09), see promotion.md §10.

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-06-04 | Consent model for using tenant repository content in the shared golden set | per-tenant opt-in / platform policy / per-task approval | Per-tenant opt-in once; withdrawal erases that tenant's tasks; `restricted` data never used | Product, WS-09, legal |
| OQ-06-05 | Annual eval budget: ≈ $0.9M (modelled tokens) to $1.5M (2× tokens) per year for 4 T2 + 1 T3 per week plus daily sentinels | fund as projected / weekday sentinels / T3 only for models | Fund for two quarters, then re-decide on measured cost per case | Engineering leadership |
| OQ-06-08 | Minimum 72-hour canary for prompt-only changes | 72 h / 24 h / risk-based | Keep 72 h; 24 h halves canary detection power | Product, release managers |
| OQ-06-09 | Which tenants form the early release ring | volunteers / platform-chosen | Volunteers carrying 10–20% of traffic, at least one per major codebase type | Tenant admins, release managers |
| OQ-06-11 | C-STATE rule 5 forbids derived copies as authority for decisions. Guardrail rollbacks read projections and Prometheus. Is a fail-safe action (revert to known-good) acceptable on derived data? | accept / require Temporal-sourced counts | Accept: the action is fail-safe; promotion still needs system-of-record evidence and human sign-off | Orchestrator |
| OQ-06-13 | Holdback length for model changes | 7 days / 30 days | 30 days (merge-rate MDE 7.9 pt vs 16.4 pt) | Release managers |

## WS-03 — 2026-10-24T14:00Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-03-01 | Does the enterprise AI gateway provide protocol pass-through (Anthropic Messages, OpenAI Responses) and accept the Replicant shim as a service client? Does it implement GW-1–GW-12 natively? (refines OQ-00-09) | (a) Replicant shim in front (default); (b) gateway implements GW-1–12; (c) direct provider access | (a); revisit (b) only if the owner confirms per-request dynamic budgets from an external JWT plus usage export | Platform owner of the AI gateway, WS-03 |
| OQ-03-02 | Accept **Challenge: D-12** (spec/03-agents-and-tools/adr/008-challenge-d12-kata-memory-requests.md): memory requests at the median peak overcommit hosts under Kata, whose guest memory is not returned without free page reporting | keep D-12; Kata memory request = limit; enable `reclaim_guest_freed_memory` + cache drop; measure then decide per runtime class | Measure host footprint in the first baseline; adopt `match_limit` for a Kata class when p50 footprint ≥ 0.8 × limit | Orchestrator, with WS-02 evidence |
| OQ-03-03 | Default treatment of a target repo's own `CLAUDE.md` / `AGENTS.md` / `.claude/rules` | load / ignore / quote as untrusted context | Quote as untrusted context (ADR-03-007) | Product (orchestrator), WS-04 |
| OQ-03-04 | Provider data retention (zero-data-retention) per model and per-tenant data-classification ceilings for model use | per-model `zdr` flag in price book + `dataClassificationCeiling` in budgets / none | Both | WS-09 with legal |
| OQ-03-05 | Source of truth for model prices used for budgets | enterprise gateway export / contract sheet / provider list | Enterprise export when present, otherwise contract sheet via admin API; nightly reconciliation | FinOps |

## WS-04 — 2026-10-24T14:06Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-04-01 | May repo- and path-scoped rules auto-activate (no admin) after the PR merged, under admin-configured policy with 14-day probation and 20% holdout? | admin approval for every entry; auto with probation | Auto with probation (an admin queue for 1,500 repos would not keep up; probation bounds damage; D-25 satisfied by policy) | Orchestrator (product) |
| OQ-04-02 | Withhold memory to measure it: 2% of jobs get no pushed memory; probation entries are withheld from 20% of eligible jobs | 0% / 2% / 5%; 0–20% | 2% and 20%; these are the only unbiased measurements | Product |
| OQ-04-03 | Non-admins' memory view: show who wrote the source comment? | show / hide | Hide (entries are team conventions; avoids profiling and friction) | Product, WS-07 |
| OQ-04-04 | Preferences: allow team-scoped style preferences, never per-person? | none / team / per-person | Team-scoped only (ADR-04-005) | Product |
| OQ-04-05 | Embedding provider through the AI gateway | Voyage `voyage-code-4` / OpenAI `text-embedding-3-large` / internal model | Voyage code model at 1,024 dims; swappable per embedding space | Platform owner, WS-03 |
| OQ-04-06 | Should safety-class quarantines auto-cancel affected running jobs? | never / notify + one-click cancel / auto | Notify + one-click cancel | WS-09, orchestrator |

## WS-05 — 2026-10-24T14:12Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-05-01 | After the QA fix budget is exhausted, open the publish gate with failing QA (draft PR only) or fail the job? | gate with failure / fail job | Gate with failure (draft only); fail for maintenance children; tenants may tighten | Product, WS-01, WS-07 |
| OQ-05-04 | Allow `allowPrivilegeEscalation: true` on the `builder` sidecar (setuid `newuidmap`) in Kata `qa_build` pods? | allow / deny (QA does not build images; CI does) | Allow: the escalation stays inside the guest; seccomp and caps already relaxed there by the VAP | WS-09 |
| OQ-05-10 | How does an approver ask for QA to be re-run after operators fix an environment definition? | gate decision mapped to a QA re-run / new job-control Update (contract change) / reject and re-trigger | Gate decision `request_changes` with a reason code that WS-01 maps to a QA re-run | WS-01, WS-07 |
| OQ-05-11 | Should a test that becomes flaky only on the change (`flaky_new`) block? | block / warn | Block by default (usually a real race), tenant-overridable | Tenant owners |
| OQ-05-17 | Render QA service readiness as harness waits instead of sidecar `startupProbe`? | startupProbe / harness wait | Harness wait for role qa, so failures produce evidence | WS-02, WS-03 |
| OQ-05-18 | Put QA service data on the enforcing workspace device instead of separate `dataVolume` emptyDirs? | separate emptyDirs (eviction) / workspace directories (ENOSPC) | Workspace directories when `guestBlockDevice` is in force (mount the whole volume, point data dirs into it) | WS-02, WS-03 |
| OQ-05-19 | Offer Testcontainers (rootless Podman Docker API) later? | never / per-tenant opt-in once a `/dev/net/tun` device path without `privileged` exists | Revisit after WS-02 proves a device path; not in v1 | WS-09, WS-05 |
| OQ-05-08 | Should visual diffs block for UI-owning tenants? | advisory / block unexpected changes | Advisory in v1 | Tenant owners |

## WS-02 — 2026-10-24T16:45Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-02-01 | Accept EC2 nested virtualization (L1 KVM on Nitro) as the production layer under Kata | nested virt `m8id`; bare metal only | Nested virt, with automatic fallback to `m8id.metal-48xl` if benchmark gates fail | WS-09, orchestrator |
| OQ-02-02 | Accept the `guestBlockDevice` mechanism (NRI plugin + Kata direct-volume) given it uses a Kata interface in an uncommon way | accept with CI gate; virtio-fs + XFS quota only | Accept; Kata upgrades gated on B-DISK tests | WS-09, WS-02 |
| OQ-02-03 | WS-03 Challenge: D-12 (Kata memory requests) | keep D-12; measured `match_limit` switch | WS-02 supports the challenge; density already assumes limit-committed memory for Kata; B-MEM-01 supplies evidence | Orchestrator |
| OQ-02-04 | Spot capacity for sandbox pools | on-demand only; spot for `utility` | On-demand only in v1 (at-most-once runs make interruptions expensive) | WS-10 |
| OQ-02-05 | Residual token validity after a run ends (≤ 60 min, one audience) | accept; verifiers check run state per request | Accept plus grant revocation list (C-RUNNER 2.1.0) | WS-09 |
| OQ-02-06 | Auto-cancel runs with repeated denied egress (`security` cancel reason) | alert only; cancel after N denials | Alert only in v1; revisit with false-positive data | WS-09 |

## WS-07 — 2026-10-24T14:14Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-07-01 | Live Okta group read (Users API) on every gate decision, in addition to the SCIM replica | live read with bounded replica fallback / replica only | Live read; replica fallback only if fresh (< 5 min) and risk < critical; otherwise deny `identity_unverifiable` (identity.md §10) | WS-09, identity owners |
| OQ-07-02 | Slack one-click approval for `critical` gates | allow / UI only | UI only ("Review in Replicant" button) | WS-09, product |
| OQ-07-03 | Two-person rule for critical **job** gates | single winner (C-MSG) / quorum (C-MSG change) | Single winner in v1; GitHub required reviews are the independent second person | Product, orchestrator |
| OQ-07-04 | Default GitHub decision surface | issue `/replicant` commands / draft-PR check-run actions | Issue commands; check-run actions opt-in per tenant (ADR-07-005) | Product |
| OQ-07-05 | GitHub identity linkage source | GHEC SAML external identities / EMU SCIM / OAuth only | SAML external identities where available (zero-click), OAuth link otherwise | Enterprise GitHub owners |
| OQ-11-04 (decision) | Local dev seed token | — | **Decided (WS-07 with WS-09): adopt WS-09 secrets.md §11 in full** — build-time exclusion (`replicant_dev_auth` only in dev images, CI proves absence), dev-only signing key rejected by prod admission, startup guard, identity-service denies `svc_dev-seeder` outside local, scope limited to seed endpoints, no Helm value, P1 on any non-local use. Runtime flag alone rejected. identity.md §9.3 | WS-07, WS-09 |
| OQ-07-06 | Requester deprovisioned while a job runs | pause and notify tenant admins / cancel / reassign to manager | Pause and notify tenant admins | Product |

## WS-08 — 2026-10-24T14:22Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-08-01 | Observability tenancy: Loki tenants by data class with an enforced fixed-selector proxy (ADR-08-002), or per Replicant tenant `t-<tenant>` org IDs (WS-09 tenancy.md §8)? | data class; per tenant | Data class: Grafana OSS cannot enforce per-tenant data source access anyway; both WS-09 G-08 conditions are implemented | Orchestrator with WS-09 |
| OQ-08-02 | License Langfuse Enterprise (project RBAC, SCIM, retention, audit log)? | OSS + Replicant sweeper and proxy; EE | OSS for v1; revisit if engineers need direct Langfuse access | Budget owner, WS-09 |
| OQ-08-03 | Tempo HA (microservices + Kafka) from day one? | monolithic; microservices | Monolithic until 50 GB/day or SLO relevance | WS-08, WS-10 |
| OQ-08-06 | May the LeakPurge workflow hold the leaked value KMS-sealed for its lifetime? | sealed blob; re-supply per step | Sealed, deleted at close, never in history in clear | WS-09 |
| OQ-08-07 | Delete Temporal history of a closed job after a purge? | keep encrypted until retention; delete after redacted export | Keep; delete for restricted tenants on request | WS-09, WS-01 |
| OQ-08-10 | SLO-1 (first progress ≤ 60 s) objective | 99%; 95% | 99%, revisit after 30 days of data | Product |
| OQ-08-11 | Workflow history export retention | 2 y; 7 y; per tenant | 2 y default, 7 y per tenant policy | WS-09, compliance |

## WS-10 — 2026-10-24T16:30Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-10-01 | Is a 4 h regional-disaster RTO acceptable (RPO ≤ 1 min for databases, ≤ 15 min for artifacts)? | 4 h warm-data/pilot-light (≈ +$0.7k/month) / 1 h warm compute (≈ +$5k/month) | 4 h, consistent with the 99.9% target | Product |
| OQ-10-02 | Backup-account Vault Lock mode | governance / compliance | Governance: compliance mode blocks erasure of PII inside backups for the whole lock period | WS-09, legal |
| OQ-10-03 | Tenant offboarding export window before crypto-shred | 30 / 90 days | 30 days, extendable per tenant | Product, legal |
| OQ-10-04 | Accept Temporal on Aurora PostgreSQL 16, gated by the staging soak (temporal-persistence.md §8), with fallback to RDS for PostgreSQL 16 | Aurora / RDS | Aurora (uniform DR via Global Database, faster failover) | WS-10, WS-01 |
| OQ-10-05 | Role-gated cross-tenant RLS (WS-10) vs GUC-only + BYPASSRLS `svc_operator_views` (WS-09 baseline) | — | Role-gated | WS-09 |
| OQ-09-04 (answer) | Aurora vs RDS for `replicant` | — | **Aurora PostgreSQL 17** (ADR-WS10-001), partly because Database Activity Streams exists only on Aurora for PostgreSQL (and not on Aurora 18 yet) | WS-10 (decided) |
| OQ-11-05 (answer) | KVM-capable CI runners for `k8s-kata` | EC2 nested-virt runners / bare metal | Agree with WS-11: Actions Runner Controller on a dedicated EKS node pool of nested-virtualization-capable `c8id`/`m8id` instances, in the **CI account** (never the prod core or exec accounts), scaled to zero when idle; `*.metal` (`c6id.metal`) as the fallback if nested-virt Kata proves flaky. Nested-virt availability is WS-02's verified input (V00-30); cost is about the price of one `c8id.4xlarge` per concurrent job | WS-10 (answered), WS-02, WS-11 |
| OQ-02-04 (answer) | Spot capacity for sandbox pools | — | No data-platform objection to spot for `utility`; databases, Temporal, and the migrate/maintenance Jobs stay on on-demand `platform` nodes | WS-02 |

## WS-01 — 2026-10-24T15:45Z

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-01-01 | Accept **Challenge: D-01** (ADR-01-001): LangGraph runs inside the workflow, compiled per job from the bundle; every unit of real work in a node is an explicit-ID activity; no runtime dependency on the `temporalio-langgraph` plugin | (a) accept replacement text; (b) keep "integrated through Temporal's LangGraph integration" literally (then a `PluginEngine` with graphs pre-registered at worker start; config-only graph changes need worker restarts) | (a): the plugin registers graphs at worker construction and fixes activity options per node, conflicting with D-02 and ADR-008 (verified from source); both engines pass all scenarios | Orchestrator |
| OQ-01-02 | Closed-workflow retention in the Temporal namespace | 7 / 30 / 90 days | 30 days; archival is experimental in self-hosted Temporal, so the projection, audit, approval records, and WS-08 `workflow_history_export` are the long-term record | WS-10 |
| OQ-01-03 | Should migrations to bundles that differ only by tightening configuration be auto-consented? | always ask the requester (CC-3) / auto for tightening-only | Always ask (CC-3 as written); revisit with migration acceptance data | Product, WS-06 |
| OQ-01-04 | Default after QA fix iterations are exhausted (same as OQ-05-01) | fail the job / draft-PR gate with failing QA | Both implemented (`repair_limits.qa.on_fail_after_fixes`); default `fail_job` as instructed | Product |
| OQ-01-05 | Should hotfix repair pushes require publish approval in every repair cycle? | always (`always_for: hotfix`) / risk-based | Always; hotfix repair budget is 2 cycles | WS-07 (gate_rules), Product |

## WS-06 — fix round wave 1 — 2026-10-24T17:25Z

- **OQ-06-01 closed.** C-DEF 2.4.0 adopted `ramps[]` (CCR-06-05b); WS-06 now runs two canary slots and one holdback slot (promotion.md §12).
- **OQ-06-05 updated numbers.** Projected eval spend is $0.65M per year at the modelled tokens and $1.13M at 2× tokens (4 T2 with reused baselines, 1 T3, daily sentinels). The promotion policy now caps total eval, shadow, proposal and component spend at $28k per week and $100k per month ($1.2M per year). Recommendation unchanged: fund the ceiling for two quarters, then re-decide on measured cost per case. Who decides: engineering leadership.

## WS-05 — 2026-10-24T17:45Z (wave-1 fix round updates)

| ID | Question | Options | Recommendation | Owner |
|---|---|---|---|---|
| OQ-05-04 (update) | `allowPrivilegeEscalation: true` for the QA `builder` | — | **Decided** by the orchestrator (WS-02 controller.md §4.1) | — |
| OQ-05-10 (update) | How does an approver re-run QA after an environment fix? | — | **Decided:** gate decision `rerun_qa` (CCR-05-04); fallback `request_changes` reason `qa_rerun` | WS-07, WS-01 |
| OQ-05-17 (update) | `startupProbe` for QA services | — | **Decided:** none; the harness owns readiness (XD-02-15) | — |
| OQ-05-18 (update) | Where QA service data lives | — | **Decided:** `/workspace/.services/<name>` inside the enforced workspace (XD-02-15, R1-114) | — |
| OQ-05-19 | Partition RLS gap applies to every partitioned tenant table (XD-FIX-R1-113): should `ops.apply_tenant_rls` itself recurse to existing partitions and should partition maintenance call it? | recurse in the function / call per partition in maintenance / both | Both: recursion fixes existing partitions, maintenance covers new ones | WS-10 |

## WS-01 — 2026-10-24T18:00Z (fix round wave 1)

| ID | Question | Options | Recommendation | Who decides |
|---|---|---|---|---|
| OQ-01-06 | Hard job lifetime `repair_limits.job_max_age_days` (R2-044) | 45 / 60 / 90 days | 60 days: it exceeds a 30-day gate wait plus a 30-day PR wait. Escrow and image retention are sized to 67 days (XD-FIX-R2-044) | Product, WS-06 |
| OQ-01-07 | How long a job may hold on a dependency outage before failing (R2-010) | 24 / 72 / 168 h | 72 h (`repair_limits.dependency_block.max_hours`), so a weekend outage is survived | Product, WS-10 |

## WS-05 — 2026-10-24T20:55Z (wave 3: ID collision fix, R1V-124)

| ID | Question | Options | Recommendation | Owner |
|---|---|---|---|---|
| OQ-05-20 (renumbered from the second OQ-05-19, 17:45Z) | Partition RLS gap on every partitioned tenant table (XD-FIX-R1-113): should `ops.apply_tenant_rls` recurse to partitions, and should partition maintenance secure new ones? | — | **Decided** by XD-ORCH-04 / XD-FIX-ORCH-04 (`ops.secure_partition` + hourly sweep). OQ-05-19 keeps its original meaning (Testcontainers / Docker API in QA). XD-ORCH-04's heading "from WS-05 finding OQ-05-19" refers to this item | WS-10 |
