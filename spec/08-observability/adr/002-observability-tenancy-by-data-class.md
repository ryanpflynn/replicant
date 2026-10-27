# ADR-08-002: Observability multi-tenancy by data class, not by Replicant tenant

- **Status:** Accepted (WS-08, 2026-10-24). Responds to WS-09 control review G-08 and records a disagreement
  with WS-09 tenancy.md section 8 (per-tenant Loki org IDs and per-tenant Langfuse organizations).
- **Constraints:** D-17, D-18 (no confused deputy), D-21, D-25

## Context

The brief asks whether Loki, Tempo and Mimir use `X-Scope-OrgID` per Replicant tenant (40 tenants). Relevant facts:

- Grafana OSS has no data source permissions; they are Enterprise or Cloud only [verified:
  https://grafana.com/docs/grafana/latest/administration/data-source-management/, 2026-10-24]. In OSS the only
  isolation unit is the Grafana organization. Team sync is Enterprise [verified:
  https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-authentication/okta/,
  2026-10-24].
- Loki supports multi-tenant queries (`querier.multi_tenant_queries_enabled`, default false), per-tenant limits,
  per-tenant and per-stream retention (`retention_stream`), and per-tenant deletion mode [verified: Loki 3.7
  configuration reference, docs/sources/shared/configuration.md at v3.7.9, 2026-10-24].
- A sandbox runs untrusted code that can print without bound; a log flood must not starve platform logs.
- Operators debug across tenants daily (one incident usually touches many tenants' jobs).
- Engineers must only see logs of jobs they are authorized for (D-18): the control-plane UI already performs that
  authorization per job (C-STATE section 2: "live tail of a running transcript streams from Loki on demand").

## Options

1. **Per Replicant tenant** (`t-<tenant_id>` org IDs, as WS-09 tenancy.md section 8 proposes). Hard storage
   isolation; but Grafana OSS cannot expose per-tenant data sources to tenant users anyway, so the isolation only
   benefits the control-plane proxy, which already scopes by tenant. Operators need 40-way multi-tenant queries
   on every panel; per-tenant limits must be tuned 40 times; platform logs about a job (replicant-worker,
   controller) belong to no single tenant stream or must be duplicated.
2. **By data class (chosen):** Loki tenants `platform` (Replicant and infrastructure services), `sandbox`
   (everything emitted from `sbx-*` pods), `audit` (audit mirror). Tempo and Mimir: one tenant `replicant`.
   Per-replicant-tenant scoping is a **label** (`tenant`) enforced by the only tenant-facing reader.
3. **Single tenant everywhere.** Loses flood isolation and per-class retention and deletion settings.

## Decision

Option 2, under three enforced conditions (WS-09 G-08):

1. **No direct tenant-user access to Loki, Tempo, or the operator Grafana org.** Grafana org 1 "Replicant
   Operations" (all data sources) admits Okta groups `replicant-operators`, `replicant-sre`, `replicant-admins`,
   `replicant-security` only (`[auth.okta] org_mapping`, config/grafana/values.yaml). Org 2 "Replicant Tenants" has
   only the Mimir data source; metrics carry no content and no identifiers (C-IDS section 11). The Loki, Tempo
   and Mimir query frontends accept connections only from the Grafana pods and control-plane-api
   (CiliumNetworkPolicy `observability-query-ingress`, WS-09 reviews).
2. **control-plane-api builds every tenant-facing log query itself.** Endpoint `GET /v1/jobs/{job_id}/logs`
   (WS-07) authorizes the caller for the job (Okta group plus live repo permission, D-18), then issues LogQL of
   the fixed form `{tenant="<tenant of the job>", source=~"app|transcript"} | job_id="<job_id>"` with
   `X-Scope-OrgID: platform|sandbox`, and never accepts user-supplied LogQL, label matchers or tenant headers.
   The query builder is a closed function with property tests (telemetry-model.md section 6.4); a contract test
   asserts the selector always contains `tenant=` equal to the job's tenant.
3. **Langfuse access is operator-only** (ADR-08-003); engineers see LLM observations of their own jobs through
   the control-plane UI, which proxies the Langfuse API by trace ID after the same job authorization.

**Restricted tenants are routed to their own Loki tenant (implemented, R1V-052; WS-09 tenancy.md section 8).**
node-logs.alloy sends the sandbox streams of every tenant of isolation class `restricted` to Loki tenant
`sandbox-<tenant_id>` (a `stage.match` on the `tenant` label against `LOKI_RESTRICTED_TENANTS_REGEX`, from ConfigMap
`observability-tenant-classes`, the same source as the Langfuse exclusion); labels and queries are unchanged, and
the routing is covered by config/alloy/tests/test_node_logs.py. Their platform-service lines stay in `platform`
(identifiers and redacted messages, no transcript content). control-plane-api's log proxy queries
`platform|sandbox-<tenant_id>` for such jobs (telemetry-model.md section 6); operators use data source
`Loki (restricted)`, whose `X-Scope-OrgID` is rendered from the same ConfigMap; the obs-ingress Envoy admits
`sandbox-*` tenant headers from `alloy-node.exec.<env>`. Defaults apply (30-day retention); per-tenant retention is
otherwise a `retention_stream` rule on `{tenant="<t>"}` (config/loki/runtime-overrides.yaml).

## Consequences

- One operator data source covers all tenants (`X-Scope-OrgID: platform|sandbox`).
- Sandbox floods are isolated by tenant-level rate limits (`sandbox` 10 MB/s) and per-pod `stage.limit`.
- Disagreement with WS-09 tenancy.md section 8 is recorded in `spec/_coordination/open-questions.md`
  (OQ-08-01) for the orchestrator. If per-tenant org IDs are mandated, node-logs.alloy changes one
  `stage.tenant` to derive `t-<tenant>` from the label and the operator data source lists 43 tenants; no other
  design element changes.

## Revisit when

- Tenant users are given direct Grafana access (would require Grafana Enterprise data source permissions or
  per-tenant orgs, each with its own data source pinned to `t-<tenant>`).
- A tenant's regulatory regime requires physically separate log storage.
