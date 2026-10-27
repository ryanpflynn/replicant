# Contracts Frozen

**Frozen:** 2026-10-24 by the orchestrator, after review of WS-00 output.

From this point, contracts change only through `spec/_coordination/contract-change-requests.md`,
adjudicated by the orchestrator. Accepted changes bump the version below and are announced to every
consuming workstream.

## Contract versions

| ID | Version | Files |
|---|---|---|
| C-ARCH | 2.0.0 (major, CCR-ORCH-02: product rename to Replicant) | architecture.md |
| C-STATE | 2.0.0 (major, CCR-ORCH-02: product rename to Replicant) | state-ownership.md |
| C-IDS | 2.0.0 (major, CCR-ORCH-02: product rename to Replicant) | ids-and-naming.md |
| C-RUNNER | **3.0.0** (major, CCR-ORCH-02: product rename to Replicant) | runner-api.openapi.yaml, schemas/run-result.schema.json |
| C-CRD | **3.0.0** (major, CCR-ORCH-02: product rename to Replicant) | agentrun-crd.yaml, agentrun-crd.md |
| C-MSG | 2.0.0 (major, CCR-ORCH-02: product rename to Replicant) | events-and-signals.md, schemas/workflow-messages.schema.json |
| C-JOBREQ | 2.0.0 (major, CCR-ORCH-02: product rename to Replicant) | job-request.schema.json |
| C-DEF | **3.0.0** (major, CCR-ORCH-02: product rename to Replicant) | definitions-contracts.md, schemas/definition-envelope.schema.json, schemas/release-bundle.schema.json |
| C-ART | 2.0.0 (major, CCR-ORCH-02: product rename to Replicant) | artifact-layout.md, schemas/run-manifest.schema.json |
| C-PG | 2.0.0 (major, CCR-ORCH-02: product rename to Replicant) | postgres-ownership.md |

Full per-version history is in each file's changelog and in `README.md`.

**2026-10-25, CCR-ORCH-02 (product rename to Replicant).** Every contract takes a major bump: product-named identifiers
change (domain, env prefix, headers, names, CamelCase types, the trace/span/session ID seeds). No structural change. Previous
versions: C-ARCH 1.5.4, C-STATE 1.10.2, C-IDS 1.13.4, C-RUNNER 2.5.3, C-CRD 2.4.0, C-MSG 1.7.4, C-JOBREQ 1.4.2, C-DEF 2.5.0,
C-ART 1.10.4, C-PG 1.4.3.

Applied CCRs: ORCH-02 (product rename, all contracts major); 11-01..03, 09-01..07, 03-01..04, 06-01..06, 04-01..03, 05-01..03, 07-02..05, 02-01..05, 10-01..06, 08-01..05, 01-01..05; Phase 3 wave 0 (R2-002, R2-004, R2-005, R2-016, R2-044, R1 role fixes: fourth exec role `run-notifier`, encrypt-only); third exec role `agentrun-controller`; C-DEF 2.1.1 replaces a retiring example model ID (see `spec/_coordination/contract-change-requests.md`). Phase 3 wave 1: 09-08..11, 04-04..07, 03-05..09, 05-04..05, 02-06..11, 06-07, 06-11..16, 11-04, 08-06..07, 10-07..11; conflict resolutions: legal bucket (holds on `audit` and legal bucket only), revocation feed in `ops`. Final wave-1 batch: 07-06..12, 01-06..11; shared `decision_channel` enum; strict `sch_` IDs. Wave-2 micro-batch: CCR-02-12, 10-12, 09-12, ORCH-01, 03-10; canonical scope rule (C-PG rule 4). Wave 3: CCR-00-01 (max_repos 15,000, MaintenanceShard at 2,000 repos), queue_position removed, audit-exporter rights (C-PG rule 3).
Interaction fixes accepted from WS-00: run-spec digest excludes `run_grant`; revocation list keyed by `job_id`/`run_id`
(WS-09 to confirm); full-snapshot publish only for `source: config_repo`; `experiment` ramps accept `canary`.
V06-14 verified by WS-01 (V01-01): the one-new-build-ramp limit is lifted; a candidate build must have registered
its task queues before a ramp is assigned. ADR-01-001 (LangGraph in-workflow, explicit activity IDs) accepted.
CCR-01-05 batched events are wrapped as `{seq, event: TimelineEvent}` (accepted).

## Orchestrator review

Independently re-validated: OpenAPI 3.1 (`openapi-spec-validator`), CRD YAML parse, all JSON Schemas
meta-valid (Draft 2020-12). Reviewed: state ownership (single owner per row), gate decision
validators and authoritative identity check, configuration-versus-code rules CC-1 to CC-4.

## Adjudications

| Item | Decision | Effect |
|---|---|---|
| ADR-014 Challenge: D-07 | **Accepted.** FS quota monitoring does not enforce | D-07 text amended in shared context. WS-02 selects an enforcing mechanism per runtime class (`spec.workspace.enforcement`) |
| OQ-00-02 Separate execution EKS cluster | **Accepted provisionally.** WS-02 and WS-09 may propose reversal via change request with evidence | ADR-007 stands |
| OQ-00-04 `repo-mirror` as a read-only token holder behind the broker | **Accepted.** Supersedes the WS-07 brief's two-holder statement | Token holders: token broker (sole key holder) issuing to Publisher, webhook/reply service, and read-only repo-mirror |
| OQ-00-05 Tightening-only tenant-derived bundles auto-promote | **Deferred to WS-06**, which must decide and justify against D-03 | — |
| OQ-00-09 Gateway capabilities | **Open, for the user.** WS-03 specifies the minimum gateway interface and a thin replicant-owned shim design if the internal gateway lacks per-run tokens, budgets, or usage export | — |

## Facts from WS-00 verification that Phase 2 must respect

- Langfuse is at v4; design against v4, not v3.
- Promtail is end of life; use Grafana Alloy.
- Worker Versioning is GA; upgrade-on-Continue-As-New is public preview (needs a fallback).
- The LangGraph integration is moving to the standalone `temporalio-langgraph` package (public preview).
- Self-hosted SQL visibility allows 10 Keyword search attributes; the C-IDS set fits within it.
- Temporal persistence: PostgreSQL 13–16 tested; the Replicant database may run 17+.
- EC2 nested virtualization is available on some non-metal families with local NVMe (for example
  M8id, C8id); WS-02 must re-verify and include them in the runtime evaluation.
