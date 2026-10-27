# ADR-08-003: One Langfuse organization and project per environment; one trace per job

- **Status:** Accepted (WS-08, 2026-10-24); amended in wave 1 for R2-009. This is **the single Langfuse tenancy
  model** for Replicant: WS-08 decides, WS-09 aligns tenancy.md section 8 and SC-51 to it (XD-FIX-R2-009).
  Supersedes WS-09's earlier "organization per tenant" proposal.
- **Constraints:** D-03 (prompt versions in bundles), D-17, D-21, C-IDS Rule T-1 and section 10

## Context

Facts checked on the self-hosted OSS edition (Langfuse v4):

- Project-level RBAC is Enterprise Edition only [verified: https://langfuse.com/docs/administration/rbac,
  2026-10-24].
- The SCIM and Organization Management API (create projects, API keys, memberships) is Enterprise Edition only
  [verified: https://langfuse.com/docs/administration/scim-and-org-api, 2026-10-24].
- Data retention management is Enterprise Edition only [verified:
  https://langfuse.com/docs/administration/data-retention, 2026-10-24].
- New SSO users can be placed in a default organization and project with a default role
  (`LANGFUSE_DEFAULT_ORG_ID`, `LANGFUSE_DEFAULT_ORG_ROLE`, `LANGFUSE_DEFAULT_PROJECT_ROLE`) [verified:
  https://langfuse.com/self-hosting/administration/automated-access-provisioning, 2026-10-24].
- A prompt link on an observation (`langfuse.observation.prompt.name` / `.version`) refers to a prompt in the
  same project as the trace [unverified: cross-project prompt linking; Langfuse prompts and traces are both
  project-scoped objects, and no cross-project reference is documented].
- Bundle builds and the context assembler resolve prompts from Langfuse (C-DEF section 4.3), and WS-06 datasets
  and experiments link dataset items to traces in the same project.

## Options

1. **Organization (or project) per tenant** (WS-09 tenancy.md section 8). Without the EE Organization API, 40
   organizations, their API keys and memberships must be created by hand or by direct Postgres writes (forbidden
   by D-26); OTLP export would need per-tenant credentials routed by the collector; prompts (one set, referenced
   by bundles) would have to be copied into 40 projects or prompt links break; WS-06 datasets would span projects.
2. **One organization `replicant`, one project `replicant-<env>` holding prompts, traces, scores and datasets
   (chosen).** Tenant scoping by trace tags `tenant:<id>` and metadata `replicant.tenant_id` (C-IDS section 10).
   Tenants of isolation class `restricted` are not sent to Langfuse at all (decision below).
3. **Buy Langfuse EE.** Adds project RBAC, SCIM, retention and audit logs. A budget decision outside WS-08
   (open question OQ-08-02); this ADR is compatible with it (EE would add per-tenant viewer projects later).

## Decision

Option 2:

- **Access:** the Okta application for Langfuse is assigned only to `replicant-operators`, `replicant-admins`,
  `replicant-prompt-authors` (WS-06) and `replicant-security`; `AUTH_DISABLE_USERNAME_PASSWORD=true`;
  `LANGFUSE_DEFAULT_ORG_ROLE=VIEWER`; project admins elevate prompt authors to MEMBER in the UI. Engineers do not
  log in to Langfuse; they see their job's observations in the control-plane UI, which calls
  `GET /api/public/v2/observations?traceId=<job trace id>` server-side after authorizing the job (WS-07).
- **Trace granularity:** one Langfuse trace per job, trace ID = the job trace ID (C-IDS T-1), session ID =
  `job_id`. A job lasts up to 30 days and spans several workflow runs (Continue-As-New); per-run traces would
  scatter one job across tens of traces and break the shared trace ID with Tempo. Workflow runs and AgentRuns are
  spans inside the job trace (telemetry-model.md section 3).
- **Restricted tenants are excluded (R2-009).** The core Alloy gateway's Langfuse filter drops every span whose
  `replicant.tenant_id` matches `LANGFUSE_EXCLUDED_TENANTS_REGEX` (otlp-gateway-core.alloy; tested). The value is
  rendered by the platform chart from the tenant isolation classes (WS-09 tenancy.md section 2, class `restricted`) into ConfigMap
  `observability-tenant-classes` key `langfuse_excluded_tenants_regex`, for example `^(acme-health|acme-hr)$`;
  the default `^$` excludes nothing. A tenant's change of class takes effect at the gateway's next rollout
  (config reload); spans already in Langfuse are removed by a one-off sweeper run with that tenant's trace IDs.
  Restricted tenants keep Tempo (content-free spans), Loki and S3 transcripts; their engineers see LLM steps in
  the control-plane UI from the S3 transcript, not from Langfuse. Gateway-shim generations for those tenants are
  also excluded, so their cost is visible in Mimir (`replicant_gateway_*`), not in Langfuse.
- **No per-tenant Langfuse organizations or projects, now or for restricted tenants**: per-tenant separation is
  achieved by exclusion, not by more Langfuse projects, so there is exactly one Langfuse project per environment.
- **Retention:** the Replicant Langfuse retention sweeper (session-capture.md section 9; WS-09 G-17) enforces
  90 days for traces of every tenant, including `replicant-eval` (R1-206); prompts and datasets are not affected
  (the sweeper only deletes traces). It runs as CronJob `langfuse-retention-sweeper` in namespace `langfuse`
  (R1-193).

## Consequences

- Prompt linking, datasets, experiments and traces share one project, as WS-06 requires.
- Tenant users never hold Langfuse credentials; one OTLP ingestion key per environment, held by the core Alloy
  gateway only (Secret `langfuse-otlp-ingest`).
- WS-09 aligns: tenancy.md already states one org and project per environment; XD-FIX-R2-009 asks it to widen
  "transcript events are not sent to Langfuse" to "no spans of a restricted tenant are sent to Langfuse", and to
  restate SC-51 and threat T-EXF-08 (which still say "Langfuse organization per tenant"). OQ-08-01's Langfuse half
  is closed.
- A Langfuse operator can see every non-restricted tenant's LLM observations; observations are redacted before ingestion, and
  operators already have that access through Loki. Langfuse logins are imported from the Okta System Log into the
  audit trail (audit.md section 4).

## Revisit when

- Replicant licenses Langfuse EE: then add per-tenant viewer projects for engineers who want direct access.
- A job's observation count makes trace views unusable (more than about 20,000 observations per trace).
