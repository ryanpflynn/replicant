# Agent Capability Model

**Owner:** WS-09 · **Status:** fix round wave 1 · **Date:** 2026-10-24
**Contracts consumed:** C-RUNNER (`RunTokenClaims`, sandbox listener), C-CRD (child objects,
`networkProfile`), C-ART (prefixes, artifact types), C-IDS §7 (env vars, headers), C-DEF
(`network_profiles`, `runtime_selection`), ADR-010, ADR-011.

## Purpose

This document fixes what each sandbox role can reach and do: network destinations, credentials,
file access, MCP tools, and outputs. It is designed so that a **fully prompt-injected agent cannot
push code, approve its own work, access other tenants, or exfiltrate beyond its allowed
destinations**, and it is concrete enough to generate the network policies
([policies/network/](policies/network/)) and IAM policies ([policies/iam/](policies/iam/)) from.
WS-02 renders these profiles into child objects; WS-03 materializes the CLI configuration; WS-04
implements the MCP tool scopes.

## Decisions summary

| # | Decision | Constraint | Reference |
|---|---|---|---|
| 1 | Capabilities are **structural**: absent credentials, absent routes, and absent API permissions. CLI permission rules, prompts, and injection detectors are defense in depth only | D-21 | §1 |
| 2 | One catalogue of **eight network profiles**, reconciled with WS-02 and WS-06 (R1-213): `coding-default`, `review-readonly`, `qa-with-mirror`, `utility-minimal`, `takeover`, `eval-coding`, `eval-scoring`, `eval-snapshot`. WS-09's §3 table is authoritative for contents; the binding VAP's allowlist is generated from the promoted `network_profiles` policy. A profile is a closed list of destinations; nothing reaches the internet | D-21 | §3 |
| 3 | **The agent container holds no credential at all.** Run tokens live only in WS-03's `replicant-agentd` sidecar (loopback proxy); the STS session lives in agentd (prepare only) and WS-02's uploader | D-21 | §2.3, WS-03 ADR-001 |
| 4 | Run tokens are **per audience** (CCR-09-01) and carry a core-signed **run grant** (CCR-09-02) and the requester as `acting_for` (CCR-09-03) | D-18, D-21 | §2.2 |
| 5 | MCP servers expose **read-only** tools to sandboxes. No sandbox writes memory, registry, approvals, or anything in Postgres | D-14, D-25 | §4 |
| 6 | Repo-supplied agent configuration (`.claude/`, `.mcp.json`, `CLAUDE.md`, `AGENTS.md`) never configures the CLI. Managed settings lock hooks, MCP servers, and permission rules | D-10 | §5 |
| 7 | A fixed set of **sensitive path classes** raises risk deterministically; `.github/workflows/**` can never be published by Replicant; **`ci_executed_paths` is `critical` and non-removable** on every publish, repair push and takeover push, because target CI runs pushed code with secrets before review (R2-001) | D-19 | §6, §8 |

```mermaid
flowchart LR
  subgraph POD["AgentRun pod (one Kata VM or gVisor sandbox)"]
    direction TB
    EP["agent: entrypoint + CLI<br/>uid 10001<br/>no credentials"]
    AG["replicant-agentd (WS-03)<br/>uid 10000<br/>token set (per audience)"]
    UP["uploader + shipper sidecar<br/>uid 10002<br/>tokens: runner-sandbox (credentials:aws)<br/>STS session (run prefix only)"]
    WD["disk watchdog<br/>uid 10003, no tokens"]
    WS[("/workspace<br/>hard-limited")]
    OUT[("/run/replicant/out<br/>agent writes, uploader reads")]
    EP --- WS
    EP --> OUT --> UP
    EP -->|"loopback 127.0.0.1:7800-7803"| AG
  end
  AG -->|"443, aud=ai-gateway"| GW["AI gateway"]
  AG -->|"443, aud=memory-mcp (read-only tools)"| MM["memory-mcp"]
  AG -->|"443, aud=context-mcp (read-only tools)"| CM["context-mcp"]
  AG -->|"8443, aud=repo-mirror, upload-pack only"| RM["repo-mirror"]
  EP -->|"443, anonymous read"| AF["Artifactory (curated)"]
  UP -->|"443, aud=runner-sandbox"| RS["runner-sandbox listener"]
  UP -->|"443, STS via sandbox VPCE"| S3["S3 run prefixes"]
  UP -->|"4318"| OT["otel-sandbox"]
  EP -.->|"denied"| X["internet, IMDS, kube-apiserver,<br/>other sandboxes, GitHub, Temporal, Postgres"]
```

---

## 1. Invariants and how each is enforced

| Invariant | Structural enforcement (all must fail for the invariant to break) | Policy file |
|---|---|---|
| **I-1 Cannot push code** | No Git credential of any kind in the pod; repo-mirror serves `git-upload-pack` only and rejects `git-receive-pack`; no route or DNS answer for `github.com`; the Publisher (core) publishes only the approved diff-bundle digest | [cnp-run-egress.coding-default.yaml](policies/network/cnp-run-egress.coding-default.yaml), [ccnp-sandbox-baseline.yaml](policies/network/ccnp-sandbox-baseline.yaml) |
| **I-2 Cannot approve its own work** | No Temporal, Slack, GitHub, Jira, or control-plane credential; no route to them; gate validator V8 requires `usr_` actors; the decision handler re-checks identity through identity-service | C-MSG V8; [authorizer.go](policies/temporal/authorizer.go) |
| **I-3 Cannot access other tenants** | Token `tenant_id` + run grant checked by every verifier; STS session resources come from the session's own tags; Cilium denies east-west; per-tenant namespaces and KMS keys; RLS in Postgres behind MCP | [sandbox-artifacts.permissions.json](policies/iam/sandbox-artifacts.permissions.json), [vap-agentrun-binding.yaml](policies/kubernetes/vap-agentrun-binding.yaml) |
| **I-4 Cannot exfiltrate beyond allowed destinations** | Sandbox subnets have no default route; Cilium default deny with FQDN allowlists; DNS proxy answers only allowlisted names; STS usable only through the sandbox VPC endpoint whose policy allows only Replicant run prefixes | [vpce-s3-exec-sandbox.policy.json](policies/iam/vpce-s3-exec-sandbox.policy.json), [scp.replicant-exec-account.json](policies/iam/scp.replicant-exec-account.json) |
| **I-5 Cannot outlive its run** | Tokens ≤ 60 min, refresh refused after terminal; STS ≤ 1 h; pod and VM deleted; Secret deleted at terminal | C-CRD §5.3 |
| **I-6 Cannot change Replicant behavior** | No write access to the registry, memory, policies, prompts, caches, images; outputs are artifacts read as untrusted data | §4 |

## 2. Per-role capability matrix

### 2.1 Summary

| Role | Workload class | Network profile | Agent token audiences | Agent scopes | Uploader | Workspace | MCP tools | Gateway | Live steering | Outputs the workflow accepts |
|---|---|---|---|---|---|---|---|---|---|---|
| `planner` | `coding` | `coding-default` | ai-gateway, memory-mcp, context-mcp, repo-mirror | `gateway:invoke`, `memory:read`, `memory:search`, `context:read`, `code_search:read`, `repo:fetch` | `credentials:aws`, `inbox:read` | read-write (changes discarded: no diff collected) | memory: `memory_search`, `memory_get`, `similar_past_work`, `memory_list_injected`; context: all read tools (WS-04 `mcp/*.tools.json` names) | yes, `budget_usd` ≤ agent limit | if `steering.live` | `plan`, `handoff`, `run_result` |
| `implementer` | `coding` | `coding-default` | same as planner | same | same | read-write | same | yes | yes (Claude stream-json) | `diff_bundle`, `handoff`, `cli_session_state`, `run_result` |
| `repair` | `coding` | `coding-default` | same | same | same | read-write | same, with `memory_search` limited to the job's snapshot | yes | yes | `diff_bundle`, `handoff`, `cli_session_state`, `memory_candidates` (written by the entrypoint from the triaged comments in inputs, never from agent prose), `run_result` |
| `reviewer` | `review` | `review-readonly` (gateway, context-mcp, repo-mirror, package mirror; **no memory-mcp**) | ai-gateway, context-mcp, repo-mirror | `gateway:invoke`, `context:read`, `code_search:read`, `repo:fetch` | `credentials:aws` | read-write scratch (tests may write); **no diff collected** | context read tools; **no memory MCP** (reviewer judges the change, not the conventions it was told) | yes | no (`codex exec`) | `review_report`, `run_result` |
| `qa` | `qa_build` | `qa-with-mirror` | ai-gateway (only if WS-05 uses an advisory agent), repo-mirror | `gateway:invoke` (optional), `repo:fetch` | `credentials:aws` | read-write; fresh checkout + approved diff; edits discarded | none | optional, small budget | no | `qa_report` (harness-computed verdict), `junit_xml`, `build_log`, `health_log`, `screenshot`, `playwright_trace`, `video`, `a11y_report`, `sbom`, `image_metadata`, `secret_scan_report`, `qa_generated_flow`, `sandbox_log`, `run_result` (XD-FIX-R1-126) |
| `takeover` | `utility` | `takeover` | repo-mirror | `repo:fetch` | `credentials:aws` | read-only restore, then archive | none | **no** | no | `workspace_archive`, `run_result` |
| `eval` | per case | `eval-coding` (agent cases), `eval-scoring` (deterministic scoring, no gateway), `eval-snapshot` (golden-task snapshot) — tenant `replicant-eval` only | as the role under evaluation; none for scoring and snapshot runs | as the role under evaluation | `credentials:aws` | as the role | as the role, against eval fixtures | eval budget (agent cases only) | per role | as the role |
| utility runs (non-takeover `utility` class) | `utility` | `utility-minimal` | ai-gateway | `gateway:invoke` | `credentials:aws` | read-write scratch | none | yes, small budget | no | role-specific (WS-02/WS-06) |

`budget_usd` = min(run limit, agent definition limit, remaining job budget, remaining tenant daily
budget), computed by the workflow and placed in the grant; the gateway enforces it (WS-03).

### 2.2 Token set per run (CCR-09-01, CCR-09-02, CCR-09-03)

The Runner issues **one token per audience**, limited to the audiences the grant lists for the
role. `replicant-agentd` (WS-03, uid 10000) receives the set as files under
`/run/replicant/tokens/<audience>` (mode 0400) from the `ar-<body>-creds` Secret, mounted only into
agentd; WS-02's uploader receives only `runner-sandbox`. Example claims for the implementer token for
`context-mcp`:

```json
{
  "iss": "https://runner.prod.replicant.internal",
  "sub": "run:run_gj3h3ep3phjfs9e23v768zrca5",
  "aud": "context-mcp",
  "exp": 1792829112,
  "iat": 1792825512,
  "jti": "rtk_01m5p7z2a4b6c8d0e2f4g6h8jk",
  "tenant_id": "payments",
  "job_id": "job_01m5p7x7r0bt69j0h0fbxepw90",
  "run_id": "run_gj3h3ep3phjfs9e23v768zrca5",
  "role": "implementer",
  "release_bundle_id": "rb_ds5p3wmcj4dag2e9a8ncp4py6k",
  "repo_ids": ["R_kgDOK2a9Xw"],
  "scopes": ["context:read", "code_search:read"],
  "acting_for": "usr_01knk8dfr0fvjc7v0b9vhvkzbp",
  "grant_jti": "rgr_01m5p7yq3w8e2r4t6y8v0x2z4p",
  "grant": "eyJhbGciOiJFUzI1NiIsImtpZCI6InJnLTIwMjYtMTAiLCJ0eXAiOiJmYWN0b3J5LXJ1bi1ncmFudCtqd3QifQ.eyJpc3MiOiJmYWN0b3J5LXByb2QtY29yZSJ9.c2lnbmF0dXJl"
}
```

Verifier obligations. The numbering and wording follow C-RUNNER 3.0.0 (`info.description`,
"Verifier obligations"), which is authoritative; WS-09 adds the detail (R1-229). Every consumer:
AI gateway shim, memory-mcp, context-mcp, repo-mirror, runner sandbox listener.

1. **Token.** Verify the signature against the Runner JWKS; `aud` **equal** to the verifier's own
   name (string equality, never "contains"); `exp` not passed; `iat` not in the future (60 s skew).
2. **Grant.** Verify the embedded `grant` against the core run-grant JWKS
   (`https://grants.<env>.replicant.internal/.well-known/jwks.json`), and the subset relation:
   `aud ∈ grant.audiences`, `scopes ⊆ grant.scopes`, `repo_ids ⊆ grant.repo_ids`,
   `budget_usd ≤ grant.budget_usd`, `exp ≤ grant.exp`, and **equality** of `run_id`, `tenant_id`,
   `job_id`, `role`, and `acting_for` between token and grant.
3. **Revocation.** Reject the token if the grant's `job_id` or `run_id` is on `/grants/revoked`
   (polled every 60 s; entries are `job_id`s and `run_id`s, C-STATE 2.0.0).
4. **Authorization.** Authorize by `tenant_id`, `repo_ids`, `scopes`, and, for context-mcp and
   memory-mcp, `acting_for` (D-18, §4).
5. **Exec revocation feed.** Reject the token if its `run_id` appears in the exec revocation feed,
   read from runner-api `GET /v1/revocations` with a cursor (CCR-02-09, R2-030), never from S3. The
   controller adds every run at its terminal phase through `POST /internal/v1/runs/{run_id}:revoke`,
   so a partitioned sandbox cannot keep spending while its retry runs. WS-09 detail (not contract
   text): poll at least every 30 s (XD-W2-09-02).

WS-09 addition (not a contract obligation): log `jti`, `grant_jti`, source IP and the decision to
Loki (`source=audit`).

## 3. Network profiles

Every profile is rendered into `ar-<body>-egress` (C-CRD §6) on top of the cluster-wide baseline
([ccnp-sandbox-baseline.yaml](policies/network/ccnp-sandbox-baseline.yaml)), which denies IMDS,
the Pod Identity agent, link-local, the VPC resolver, the API server, host, remote nodes, and every
other sandbox. Ingress to sandboxes is always denied.

Authoritative catalogue (R1-213; XD-FIX-R1-213 tells WS-02 and WS-06 to match it). "Always-on" =
DNS proxy, runner-sandbox listener (uploader), OTLP endpoint, and the S3 run buckets listed.

| Destination | Port | Mechanism | coding-default | review-readonly | qa-with-mirror | utility-minimal | takeover | eval-coding | eval-scoring | eval-snapshot |
|---|---|---|---|---|---|---|---|---|---|---|
| kube-dns (DNS proxy with name allowlist) | 53 | `toEndpoints` + `rules.dns.matchName` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `runner-sandbox.<env>.replicant.internal` | 443 | `toFQDNs` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `otel-sandbox.<env>.replicant.internal` (WS-08 sandbox OTLP, TLS; CoreDNS rewrite to Service `replicant-observability/alloy-sandbox-otlp`) | 4318 | `toEndpoints` on the backend pods (XD-FIX-R1-041); `toFQDNs` to an internal NLB under WS-02 fallback rule 6 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| S3 `artifacts`, `transcripts` run prefixes | 443 | `toFQDNs` per bucket host + sandbox VPCE policy | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| S3 `session` run prefix | 443 | same | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ | ✗ | ✗ |
| `ai-gateway.<env>.replicant.internal` (replicant-gateway-shim) | 443 | `toFQDNs` (PrivateLink) | ✓ | ✓ | only if QA agent tasks are on | ✓ | ✗ | ✓ | ✗ | ✗ |
| `memory-mcp.<env>.replicant.internal` | 443 | `toFQDNs` (PrivateLink) | ✓ | ✗ | ✗ | ✗ | ✗ | ✓ (eval fixtures) | ✗ | ✗ |
| `context-mcp.<env>.replicant.internal` | 443 | `toFQDNs` (PrivateLink) | ✓ | ✓ | ✗ | ✗ | ✗ | ✓ (eval fixtures) | ✗ | ✗ |
| repo-mirror (`repo-mirror.<env>.replicant.internal`) | 8443 pod port, or 443 via the internal NLB under WS-02 fallback rule 6 | `toEndpoints`, or `toFQDNs` to the NLB | ✓ | ✓ | ✓ | ✗ | ✓ (prepare restores the checkout before archiving) | ✓ (eval repos only, fast-path §4.3) | ✓ (eval repos only) | ✓ (eval repos only) |
| `artifactory.acme.internal` language repos (curated virtual repos, §7) | 443 | `toFQDNs` | ✓ | ✓ (reviewers run tests, WS-03 §6) | ✓ | ✗ | ✗ | ✓ | ✓ | ✗ |
| `artifactory.acme.internal` container-image (`docker-virtual`, path-style mirror for docker.io, ghcr.io, gcr.io, quay.io, mcr.microsoft.com) and Playwright repos; if B-BUILD-03 shows BuildKit needs per-registry subdomains, `qa-with-mirror` adds exactly five exact `matchName` entries: `dockerhub`, `ghcr`, `gcr`, `quay`, `mcr` `.artifactory.acme.internal`; no wildcard, which would let a sandbox send arbitrary labels to the corporate resolver (XD-FIX-R1-115, XD-W3-R1V-097, WS-02 network.md §5) | 443 | `toFQDNs` (same host; path-level access is Artifactory permission) | ✗ | ✗ | ✓ | ✗ | ✗ | as qa-with-mirror for QA cases | ✓ | ✗ |
| Anything else, including github.com, public registries, Temporal, Postgres, Okta, Slack | — | default deny | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

Reconciliation decisions (WS-09 leads, R1-213): `review-readonly` gets the package mirror and **not**
memory-mcp (reviewers run tests; a reviewer that sees the conventions it was told cannot judge them
independently), so WS-02's test 39 must expect the mirror allowed and memory-mcp blocked.
`takeover` gets repo-mirror, because `prepare` must restore the checkout before the archive is built.
`eval-isolated` is withdrawn. eval-* profiles are reserved for tenant `replicant-eval` (binding VAP).

Notes:

- **Why FQDN plus no internet route.** Cilium enforces `toFQDNs` on the IPs its DNS proxy observed.
  Several names can share an IP (S3 is the extreme case), so FQDN policy alone does not stop a
  sandbox from reaching other names on a shared IP. The sandbox subnets therefore have no default
  route at all; every allowed destination is a VPC endpoint or PrivateLink endpoint in the exec VPC,
  and each endpoint authenticates the caller. Cilium is the per-run allowlist, the route table is
  the hard floor `[unverified: Cilium documentation on IP-sharing behavior of toFQDNs; inferred from
  IP-based enforcement as documented at https://docs.cilium.io/en/stable/security/dns/]`.
- **DNS.** `*.` patterns in `matchPattern` require at least one label [verified:
  https://docs.cilium.io/en/stable/security/dns/, 2026-10-24]. The profiles use exact `matchName`
  only, so a sandbox cannot tunnel data through arbitrary subdomains of an allowed zone. Musl-based
  images try search-domain expansions; the controller sets `dnsConfig.options: ndots:1` and
  `searches: []` on sandbox pods so allowlisted names resolve without expansions.
- **QA sidecar services** (databases, queues, stubs) run inside the same pod or VM per WS-05 and talk
  over loopback; no profile entry is needed. Docker-socket approaches are not available: there is no
  Docker daemon, and VAP forbids hostPath.
- **Takeover** has no gateway and no package mirror: it only restores the checkout (repo-mirror), archives, and uploads.

## 4. MCP tool scopes

The run token decides which tools a sandbox may call; the MCP server enforces it, not the CLI.

| Server | Tool (WS-04 names them) | Required scope | Additional checks | Writes |
|---|---|---|---|---|
| memory-mcp | `memory_search`, `memory_get`, `similar_past_work`, `memory_list_injected` (WS-04 `mcp/memory-mcp.tools.json`) | `memory:read` / `memory:search` | Results restricted to the job's pinned snapshot plus `episodic` entries in scopes the snapshot policy allows; tenant from the token, never from arguments | **none** |
| context-mcp | `briefing_get_section`, `catalog_get_service`, `catalog_search`, `contracts_list`, `contract_get`, `contract_consumers`, `dependency_graph`, `owners_for_paths` (`context:read`); `code_search`, `find_call_sites`, `file_get` (`code_search:read`) (WS-04 `mcp/context-mcp.tools.json`) | `context:read`, `code_search:read` | Repository filter = repos in the tenant **∩** repos `acting_for` can read (identity-service `authorize(principal, repo, read)`, cached 60 s for reads only, never for decisions); `briefing_get_section` only for this job's briefing | **none** |
| repo-mirror (not MCP) | `git-upload-pack` | `repo:fetch` | `repo_id ∈ token.repo_ids`; run is non-terminal (`GET /v1/runs/{run_id}` cached 30 s); objects served from the tenant's cache partition | **none** (`git-receive-pack` → 403 + alert) |
| runner-sandbox listener | token refresh, AWS credentials, inbox poll/ack | `credentials:aws`, `inbox:read` | Only for `sub` run, only while non-terminal | inbox ack state |

No sandbox-facing MCP server has a tool that writes memory, candidates, the registry, approvals,
jobs, or any Postgres table. Memory candidates leave a run only as an artifact
(`memory_candidates`) that the Memory Writer activity, outside the sandbox, validates against WS-04's
content rules.

## 5. CLI configuration hardening (with WS-03)

WS-03 renders the CLI configuration ([03 cli-invocation.md §2.3–§2.5, §3.2](../03-agents-and-tools/cli-invocation.md)).
WS-09's minimum requirements, which that rendering must keep satisfying on every CLI upgrade
(WS-03's adapter contract tests assert them, and WS-06 runs them before bundle promotion):

| # | Requirement | Claude Code mechanism | Codex mechanism | Status in WS-03 draft |
|---|---|---|---|---|
| CLI-1 | Security config is read-only to the agent and outranks every repo or user file | `/etc/claude-code/managed-settings.json` on a read-only mount (managed is the highest precedence [verified: https://code.claude.com/docs/en/settings, 2026-10-24]) | `-c` overrides on the command line | met |
| CLI-2 | No repo-supplied hooks | `allowManagedHooksOnly: true` | no hook mechanism used | met |
| CLI-3 | No repo-supplied MCP servers | `disabledMcpjsonServers: ["*"]` + `--strict-mcp-config`; WS-09 also requires `allowManagedMcpServersOnly: true` (managed MCP allowlist only) [verified (index): https://code.claude.com/docs/en/settings-reference.md, 2026-10-24] | `mcp_servers` only from `-c` overrides | partly: add `allowManagedMcpServersOnly` (gap G-02) |
| CLI-4 | No repo-supplied permission rules | `allowManagedPermissionRulesOnly: true` | n/a | gap G-02 (WS-03 relies on deny rules and `dontAsk`; `allowManagedPermissionRulesOnly` closes repo `allow` additions) |
| CLI-5 | Repo instruction files not loaded as instructions | `instructionFiles: "managed-only"`, `claudeMdExcludes` | `project_doc_max_bytes = 0` | met |
| CLI-6 | No bypass mode | `disableBypassPermissionsMode`, `disableAutoMode` | `--sandbox danger-full-access` is acceptable because the AgentRun is the boundary [verified: https://learn.chatgpt.com/docs/non-interactive-mode, 2026-10-24] | met |
| CLI-7 | No tools that reach vendor-hosted services or the web | `--tools` allowlist + deny (`WebFetch`, `WebSearch`, `RemoteTrigger`, `SendMessage`, `Artifact`, …) | web search disabled by override | met; these calls would also fail at the network layer |
| CLI-8 | No credential in the agent container | agentd loopback proxy | agentd loopback proxy | met in WS-03; gap G-01 in WS-02's pod example |
| CLI-9 | No auto-update, telemetry, or error reporting egress | `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_ERROR_REPORTING` | n/a | met |
| CLI-10 | The run records which repo agent-config files were present | `InstructionsLoaded` hook + prepare scan | prepare scan | met |

These settings are **not** the boundary (an agent with a shell can do anything its UID can); they
keep repo content from reconfiguring the agent and keep behavior reproducible. The boundary is §1.

## 6. Sensitive path classes and change rules (with WS-07's `risk_rules`)

These are deterministic inputs to D-19 risk classification. WS-07 owns `risk_rules`; this is the
minimum security content WS-09 requires in the base bundle, in WS-07's schema shape. Tenant layers may
add entries, never remove them (built into WS-07's rule `ci.executed_paths` and the ADR-013 tightening
check).

```yaml
# Policy.spec for policyType risk_rules, valid against WS-07's
# 07-control-plane-and-ux/schemas/policy-risk-rules.schema.json (R1V-171). This is the security floor
# WS-09 requires in the global layer; WS-07's examples/risk-rules.global.yaml carries it plus WS-07's
# own rules. ci.executed_paths and tests.changed are also built in (non-removable). Evaluated by the
# Publisher on publisher.publish, publisher.push_update and publisher.push_takeover_branch against the
# exact pushed diff.
policyType: risk_rules
default_level: low
never_publish_paths: [".github/workflows/**", ".github/actions/**"]
# Tenants extend ci_executed_paths in their layer; WS-04's ci-workflow lint view
# (ecosystem.v_ci_workflow_lint) proposes entries, admin-applied.
ci_executed_paths:
  - "**/conftest.py"
  - "**/Makefile"
  - "**/*.mk"
  - "**/GNUmakefile"
  - "**/justfile"
  - "**/Justfile"
  - "**/Taskfile*"
  - "**/Rakefile"
  - "**/Earthfile"
  - "scripts/**"
  - "ci/**"
  - ".ci/**"
  - "tools/ci/**"
  - "**/package.json"
  - "**/.npmrc"
  - "**/.yarnrc*"
  - "**/pyproject.toml"
  - "**/setup.py"
  - "**/setup.cfg"
  - "**/tox.ini"
  - "**/noxfile.py"
  - "**/pytest.ini"
  - "**/build.gradle*"
  - "**/settings.gradle*"
  - "**/gradle/wrapper/**"
  - "**/pom.xml"
  - "**/build.rs"
  - "**/Cargo.toml"
  - "**/go.mod"
  - "**/Dockerfile*"
  - "**/docker-compose*.y*ml"
  - "**/compose*.y*ml"
  - ".husky/**"
  - ".pre-commit-config.yaml"
  - ".github/**"
  - "Jenkinsfile"
  - "**/Jenkinsfile"
  - ".gitlab-ci.yml"
  - ".circleci/**"
  - ".buildkite/**"
  - "azure-pipelines.yml"
  - ".drone.yml"
  - "bitbucket-pipelines.yml"
  - ".travis.yml"
rules:
  - id: ci.executed_paths
    description: Change touches a path target CI executes (built-in, non-removable, R2-001)
    level: critical
    when: {ci_executed_paths_touched: true}
  - id: tests.changed
    description: Test code changed; CI executes it (built-in floor, R2-001 fix 3)
    level: medium
    when: {test_files_changed: true}
  - id: diff.unicode_controls
    description: An added line contains a bidi control or zero-width character (R2-007, R1V-175)
    level: high
    when: {diff_unicode_controls: true}
  - id: security.sensitive_critical
    description: WS-09 raise_to_critical class (ownership, infrastructure, build and registry config)
    level: critical
    when: {paths_any: ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".github/dependabot.yml", "**/*.tf", "**/terraform/**", "**/helm/**/values*.yaml", "**/k8s/**", "**/Dockerfile*", "**/.npmrc", "**/.pypirc", "**/settings.xml", "**/go.mod"]}
  - id: security.sensitive_high
    description: WS-09 raise_to_high class (auth, crypto, migrations, lockfiles, agent configuration files)
    level: high
    when: {paths_any: ["**/auth/**", "**/crypto/**", "**/security/**", "**/migrations/**", "**/package.json", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock", "**/requirements*.txt", "**/poetry.lock", "**/uv.lock", "**/Cargo.lock", "**/go.sum", ".claude/**", ".mcp.json", "**/CLAUDE.md", "**/AGENTS.md"]}
  - id: diff.too_large_for_no_gate
    description: WS-09 diff rule (more than 25 files or 600 changed lines)
    level: medium
    when: {any_of: [{diff_files_changed_gt: 25}, {diff_lines_changed_gt: 600}]}
  - id: diff.binary
    description: Binary files changed
    level: high
    when: {binary_files_changed: true}
  - id: deps.new_external
    description: A new third-party dependency is introduced
    level: high
    when: {new_external_dependency: true}
  - id: tests.deleted_or_skipped
    description: Tests deleted or newly skipped vs base (QA test inventory, R2-006)
    level: high
    when: {deleted_or_skipped_tests: true}
  - id: plan.outside_paths
    description: Files changed that the approved plan did not name
    level: high
    when: {outside_plan_paths: true}
  - id: secrets.finding
    description: Secret scanner finding (publish is blocked independently)
    level: critical
    when: {secret_scan_finding: true}
```

**Default test globs** for the `test_files_changed` fact (the codebase binding's default when it
names none; WS-07's schema cites this list). This is fact-computation input, not a `risk_rules`
field:

```yaml
test_files_changed_default_globs:
  - "**/test/**"
  - "**/tests/**"
  - "**/testdata/**"
  - "**/__tests__/**"
  - "**/*_test.go"
  - "**/test_*.py"
  - "**/*_test.py"
  - "**/*.test.ts"
  - "**/*.test.js"
  - "**/*.spec.ts"
  - "**/*.spec.js"
  - "**/src/test/**"
```

**Unicode-control fact (R1V-175).** `diff_unicode_controls` is true when an added line contains a
bidi control (U+202A–U+202E, U+2066–U+2069) or a zero-width character (U+200B–U+200D, U+2060,
U+FEFF). It is the same scan that draws the approval packet's fact chips (WS-07 approvals.md A-10),
and rule `diff.unicode_controls` raises the job to at least `high` (Trojan Source, T-PI-13).

A secret-scan finding also blocks the publish outright (data-handling.md §4); the rule above only
records the level.

A `critical` risk always opens a publish gate before the push, so a human sees every change to a
CI-executed path before target CI can run it. This bounds T-PI-13 to the approver (R-02, R-05).

## 7. Package mirror curation (SC-36)

Requirements on the Artifactory configuration that sandboxes use (WS-02 operates it). These apply
to **all tenants** unless marked otherwise (R2-003).

| Setting | Value | Threat |
|---|---|---|
| Sandbox access | Anonymous **read** on the curated virtual repos only (`npm-virtual`, `pypi-virtual`, `go-virtual`, `maven-virtual`, `cargo-virtual`, `docker-virtual`), only from the exec VPC endpoint; no deploy/publish permission; the `*-remote` repos are not readable from sandboxes (control-review G-10) | T-SC-01 |
| Upstreams | Only `registry.npmjs.org`, `pypi.org`, `proxy.golang.org`, Maven Central, `index.crates.io`, ECR pull-through for base images; no generic remotes, no VCS remotes | T-SC-02 |
| **Go (all tenants)** | `GOPROXY=https://artifactory.acme.internal/api/go/go-virtual`; `GOPRIVATE=<org module prefixes>` (which implies GONOPROXY and GONOSUMDB for those prefixes only), fetched over git from repo-mirror through agentd's git relay (`git config url.<relay>/.insteadOf https://github.com/<org>/`), never through the public proxy; `GOFLAGS=-mod=mod`; `GONOSUMDB` not set beyond GOPRIVATE, `GOSUMDB` on; the `go-remote` repo **include pattern** allows only module path prefixes on the allowlist below; a miss whose host is outside the allowlist is rejected locally and is a **P2 security event**; first-seen module paths are blocked | T-SC-02 (R2-003) |
| Go module path allowlist (base; tenants extend through their layer, admin-approved) | `github.com/<allowed orgs>/` (the org's own and an approved list of upstream orgs), `golang.org/x/`, `google.golang.org/`, `gopkg.in/`, `go.opentelemetry.io/`, `go.uber.org/`, `k8s.io/`, `sigs.k8s.io/`, `cloud.google.com/go/`, `github.com/aws/` | T-SC-02 |
| npm scopes, PyPI names | First-seen names blocked for all tenants (logged with run and tenant); unscoped npm names that are not in the mirror's cache require the curation gate | T-SC-02 |
| Freshness quarantine | Versions published less than 72 h ago are not served (dependency cooldown); exceptions per package by a security approver | T-SC-01 |
| Malicious package feed | Block known-malicious packages (OSV `MAL-*` advisories or the vendor's equivalent) | T-SC-01 |
| Rate limit | ≤ 50 cache misses per run per minute | T-SC-02 |

**Mechanism per ecosystem (R2-041).** Artifactory Curation/Xray features are
`[unverified: exact feature names and per-repository configurability in the installed edition]`.
The design does not depend on them. Where they are unavailable, the curation gate is
`replicant-curation-proxy`: a stateless replicant-owned Go service in the exec account
(`pkg.<env>.replicant.internal`, two replicas, behind the same PrivateLink path) that sits between
sandboxes and the virtual repos and filters each ecosystem's metadata responses. Sandboxes then
resolve `pkg.<env>.replicant.internal` instead of `artifactory.acme.internal`.

| Ecosystem | How the 72 h quarantine is enforced | How first-seen and malicious names are blocked | Decision |
|---|---|---|---|
| npm | Proxy rewrites the packument: drops versions whose `time[<version>]` is newer than 72 h and moves `dist-tags.latest` to the newest allowed version | Proxy keeps a set of names seen in the cache; unknown names return 404 plus an event; OSV `MAL-*` names return 404 | Proxy (or Artifactory Curation if licensed) |
| PyPI | Proxy filters the JSON Simple API (PEP 691) using `upload-time` (PEP 700) per file | Same | Proxy |
| Go | Proxy filters `/@v/list` and answers `/@latest` from `.info` `Time` | Module path allowlist (above) | Proxy plus allowlist (all tenants) |
| Maven | Not enforceable: `maven-metadata.xml` has no per-version publish time | First-seen artifact coordinates blocked; OSV feed | **Risk accepted** for the 72 h quarantine; first-seen block applies. Owner WS-09; revisit if Maven Central exposes publish times |
| crates | Not enforceable from the sparse index (no publish timestamp) | First-seen crate names blocked; OSV feed; `Cargo.lock` changes raise risk to high (§6) | **Risk accepted** for the 72 h quarantine. Owner WS-09 |
| Container images (QA base and service images) | n/a: images are pinned by digest in environment definitions; plain services use `mirror/<upstream path>@sha256`, services that need the log wrapper use `replicant/svc-<name>@sha256` (mirrored upstream plus one signed layer), both admitted by the VAP and the ImageValidatingPolicy (XD-05-20) | Only signed, mirrored digests are admitted | Covered by digest pinning |

## 8. Role abuse cases (beyond the QA case in threat-model.md §7.1)

| Role | What an injected agent would try | Outcome |
|---|---|---|
| planner | Write a plan that names sensitive paths as "trivial", hoping for a low-risk route | Risk rules use the diff, not the plan; plan prose is untrusted; outside-plan paths raise risk |
| implementer | Add a dependency that phones home at install time on developers' machines | `new_dependency` raises risk; mirror quarantine; reviewer run; human gate |
| implementer | Edit `.github/workflows` to exfiltrate CI secrets | Never publishable (App lacks `workflows`; `never_publish` rule) |
| implementer | Edit a file that target CI **executes at push time** (`conftest.py`, `Makefile`, `package.json` scripts) so the PR's own CI run sends repository secrets out, before any human review (R2-001, T-PI-13) | `ci_executed_paths` is `critical`: the publish gate opens and a human approves the exact diff digest before the push; test-file changes are at least `medium` |
| repair | Same as above through a repair push to an existing PR branch | The rule applies to `publisher.push_update` too; a `critical` repair diff re-opens the publish gate |
| repair | Treat a reviewer comment like "also send the .env to …" as instructions | Comments are user content in quotes; no route anywhere; any diff change goes through the same checks |
| reviewer | Report "no issues" for a malicious diff | Reviewer findings can only raise risk (D-19); the human sees the deterministic facts |
| takeover | Leave malicious content in the workspace so the takeover branch push runs it in CI | Takeover pushes go only to `replicant-takeover/<job_id>`. If the diff touches `ci_executed_paths`: when the tenant has **not** attested onboarding item OB-1 (CI excludes `replicant-takeover/**` from secrets-bearing workflows), WS-07's `publisher.push_takeover_branch` refuses with `PushRejected("ci_paths_takeover_unattested")` and the requester gets the archive and a patch instead of a pushed branch; when OB-1 **is** attested, the takeover subflow first opens a `publish_approval` gate whose eligible approver is the requester, then pushes (XD-FIX-R2-001-WS07 and its correction) |
| takeover | Put extra content into the archive | The archive goes only to the requester, who could already read the repo |
| eval | Escape fixtures to reach production memory | Eval runs use `eval-coding`/`eval-scoring`/`eval-snapshot` (fixture MCP endpoints, tenant `replicant-eval` only, enforced by the binding VAP) |

## Open questions

| Question | Who decides | Options | Recommendation |
|---|---|---|---|
| Does the QA role need gateway access? | WS-05 | none / advisory agent with small budget | None by default; an advisory agent only for failure triage, with a $2 cap |
| Should repo `CLAUDE.md`/`AGENTS.md` content reach the agent at all? | WS-04 with WS-03 | excluded / quoted in briefing / loaded as instructions | Quoted in the briefing as repo content |
| First-seen package blocking for all tenants? | WS-09 (decided in the fix round, R2-003) | restricted tenants only / all | **All tenants** for Go (allowlist), npm and PyPI. The friction cost is accepted: exceptions go through the security approver queue |

## Risks

- `ci_executed_paths` at `critical` opens a gate on many ordinary changes (lockfile-free `package.json` edits, `Makefile` tweaks). That is the price of R2-001; measure gate volume per tenant and let tenants move CI to fork-based publishing (OQ-ORCH-01) if it is too high.
- Exact `matchName` DNS allowlists must be kept in sync with service renames; a missing name fails
  closed (run fails with `RepoFetchFailed`/`GatewayUnavailable`), which is noisy but safe.
- Separate UIDs inside one VM are defense in depth only.

## Interfaces

- **Provided:** the eight network profiles of §3 (consumed by WS-02 controller rendering and the `network_profiles` policy type);
  role → token audience and scope map (WS-02 Runner, WS-03 entrypoint, WS-04 MCP); verifier
  obligations (all token consumers); sensitive path classes (WS-07 `risk_rules`); mirror curation
  requirements (WS-02).
- **Consumed:** C-RUNNER 3.0.0 `RunTokenClaims` and verifier obligations, C-CRD 3.0.0 §6, C-ART 2.0.0 §2, C-IDS 2.0.0 §7.
