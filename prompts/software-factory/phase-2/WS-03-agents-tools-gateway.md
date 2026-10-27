# WS-03: Agent Definitions, Tool/Environment Provisioning, and AI Gateway

**Output directory:** `spec/03-agents-and-tools/`
**Constraints most relevant:** D-10, D-11, D-12, D-03

## Purpose

Design how native coding CLIs are configured and invoked with no files committed to target repos,
how sandboxes are provisioned with tools and sized correctly, and how every model call is routed
through the internal AI/ML gateway.

## Required outputs

### 1. CLI invocation (`cli-invocation.md`)

For each supported CLI, verify against current documentation and specify:

- **Claude Code:** headless invocation (`claude -p`), output formats (`--output-format stream-json`
  and related flags), turn and budget limits, tool allow/deny lists, permission mode for
  non-interactive runs, `--mcp-config`, `--append-system-prompt` / system prompt options, settings
  files and their precedence (user-scope settings and `CLAUDE.md` placed in the sandbox home
  directory instead of the repo), session resume flags, hooks, gateway variables
  (`ANTHROPIC_BASE_URL` and auth variables; Bedrock/Vertex variants if the gateway fronts them), and
  OpenTelemetry export settings. Consider whether the Claude Agent SDK is a better fit than the CLI
  for some roles, and decide.
- **Codex CLI:** `codex exec` flags, JSON event output, sandbox and approval modes appropriate inside
  an already-isolated sandbox, `CODEX_HOME` and config overrides, `AGENTS.md` placement outside the
  repo, MCP configuration, provider/base URL configuration for the gateway.
- A **runner entrypoint** (the process inside the sandbox that launches the CLI): config
  materialization, prompt and memory file placement, signal handling, transcript capture,
  exit-code and result-file contract, and structured result schema returned to the workflow.

### 2. Agent definition (`agent-definition.md`)

Full JSON Schema conforming to the WS-00 envelope: CLI and version (image digest), model, role
prompts (Langfuse prompt references), tool permissions, MCP servers (context, memory, code search),
limits (turns, tokens, wall time, cost), gateway config, env, and output contract. Two complete
examples: an implementer on Claude Code and a reviewer on Codex.

### 3. Environment definition and resource profiles (`environment-definition.md`)

- Full JSON Schema: base image, packages, setup scripts, env, caches, sidecar services (for QA; align
  with WS-05), resource profile (CPU/memory requests and limits, `ephemeral-storage`, workspace disk
  `sizeLimit`, overflow policy), runtime class hint, network allowlist references.
- Profile derivation method: how baselines are measured per codebase type (Go service, Next.js app,
  Python pipeline, JVM service), the formula (requests at median peak; limits at p95 + ~30% + measured
  runtime overhead), re-measurement cadence, and automatic right-sizing proposals from observed usage
  (Prometheus data) that a human approves.
- Image strategy: base images per language, how tool definitions become images (prebuilt and cached
  by content hash vs install at start), and supply-chain controls (signing, SBOM, provenance).
- Complete example: a Next.js app with gateway config, Playwright, a 40 GiB workspace, and a Postgres
  sidecar.

### 4. Config repo and registry publishing (`config-publishing.md`)

Git-backed config repo layout, CI validation (schema, policy, dry-run materialization), publish into
the Postgres registry as immutable versions, how a Release Bundle references them, and rollback.

### 5. Gateway integration (`gateway.md`)

How each CLI is pointed at the gateway; per-run scoped credentials (issuance, TTL, revocation on run
end); headers carrying `job_id`, tenant, and trace context for attribution; budget enforcement at the
gateway (per run, per job, per tenant); model allowlists per tenant; local stub gateway (a dev
gateway that proxies to a real provider with a dev key, plus a deterministic replay/mock mode for
tests). If gateway capabilities are unknown, state the minimum interface the factory requires.

## Must answer

- Exactly which files exist in the sandbox home directory when the CLI starts, and what writes them?
- How is a CLI upgrade rolled out, evaluated, and rolled back?
- How does a reviewer agent run with read-only intent while still executing tests?
- What happens when the gateway returns rate-limit or budget-exceeded errors mid-run?

## Acceptance criteria

- Every CLI flag and variable is verified and tagged.
- Schemas validate and the examples validate against them.
- Resource profile derivation is a procedure a team can follow, not a guideline.
