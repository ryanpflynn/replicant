# WS-04: Memory System and Context Engineering

**Output directory:** `spec/04-memory-and-context/`
**Constraints most relevant:** D-13, D-14, D-15, D-09, D-03, D-21

## Why this workstream matters most

Memory decides whether the factory improves or rots over time. Good memory turns reviewer feedback
into fewer review rounds. Bad memory injects stale, contradictory, or poisoned instructions into
every job, silently, across the organization. Design it as a product with its own lifecycle,
governance, evaluation, and UI, not as a table that the feedback loop appends to.

## Scope

1. **Long-term memory**: what the factory learns and keeps across jobs.
2. **Job working memory**: what a single job carries across nodes, CLI invocations, repair cycles,
   and Continue-As-New boundaries.
3. **Context assembly and budgeting**: how briefings, memory, constraints, and task content fit
   into each agent's context window.
4. **Ecosystem context**: briefings about the target service and related services (D-13).

## Required outputs

### 1. Memory taxonomy (`taxonomy.md`)

Define each memory type with examples, source, and lifetime:

- **Rules/conventions** (semantic): "In `payments-*` repos use `zap`, not `logrus`."
- **Episodic**: records of past jobs and repairs: what was attempted, what reviewers rejected, what
  passed. Used for "similar past work" retrieval.
- **Procedural/playbooks**: step sequences that worked for a task type in a codebase ("bump a gRPC
  dependency in a Go service: regenerate stubs with `buf generate`, then...").
- **Negative knowledge**: approaches known to fail, with the evidence.
- **Preferences**: reviewer- or team-specific style preferences (scoped narrowly; decide whether
  these are allowed at all).

### 2. Data model (`data-model.md`)

Real Postgres DDL (in the `memory` schema per `postgres-ownership.md`), including:

- Entry content (structured fields plus natural-language statement), type, scope, lifecycle state,
  confidence, version, supersedes/superseded-by links, conflict links.
- **Provenance**: source PR/comment URLs, author Okta identity, job and run IDs, extraction model and
  prompt version, approving curator.
- **Usage telemetry**: injection count, jobs injected into, outcome correlation fields (did the job
  need fewer review rounds, did reviewers repeat the same comment).
- Embeddings via `pgvector` (model, dimension, index type such as HNSW with parameters) and
  full-text search (`tsvector`) for hybrid retrieval over memory entries only.
- Row-level security or equivalent tenant isolation.
- Audit and deletion: hard delete, tombstones, and how deletions propagate to derived snapshots.

### 3. Scoping (`scoping.md`)

The scope hierarchy (for example org → tenant → service group → repo → path/glob → language/
framework), inheritance and override rules, conflict resolution between scopes (most specific wins?
explicit precedence?), and who may write at each scope. Cross-tenant sharing rules (default: none
without promotion).

### 4. Write path (`write-path.md`)

From PR feedback to active memory, with a sequence diagram:

1. Feedback Ingestor normalizes review comments (filtering bots, the factory itself, and unlinked or
   unauthorized identities).
2. Extraction (an LLM call with a versioned prompt) proposes candidate entries **only when the
   comment generalizes** beyond this PR; specify the generalization test.
3. Deduplication and merge against existing entries (similarity thresholds, merge rules).
4. Contradiction detection against existing entries; conflicts are surfaced, never silently resolved.
5. Lifecycle: `candidate` → `active` → `deprecated`/`superseded`/`rejected`. Define promotion
   criteria per scope: for example, repo-scoped entries auto-activate after the repair that applied
   them was approved and merged; tenant- and org-scoped entries require a curator approval in the UI.
   Curators are members of the admin Okta group only (D-25). Any automatic transition happens under
   a policy that admins configure; no non-admin action changes an entry's state.
6. Evidence threshold: how many independent occurrences before an entry is promoted beyond repo
   scope.

### 5. Poisoning and safety (`safety.md`)

Threat model for memory: a malicious or careless commenter, prompt-injected repo content flowing into
extraction, a compromised account, gradual drift. Controls: identity-gated writes, scope limits by
author role, curator approval for broad scopes, content rules (memory may not contain secrets,
URLs outside an allowlist, instructions to disable safety controls, or tool-permission changes),
anomaly detection on write volume, quarantine, and fast rollback (disable an entry or all entries
from an author/time window in one action, effective for new jobs immediately).

### 6. Retrieval and injection (`retrieval-injection.md`)

- Retrieval: scope filter first, then hybrid ranking (BM25/FTS + vector + recency + confidence +
  usage outcomes), with formulas and defaults.
- **Push vs pull**: what is injected up front (high-confidence rules for the exact scope) vs exposed
  through a memory MCP server the agent can query on demand (episodic "similar past work"). Specify the
  MCP tool definitions.
- Injection mechanics per CLI, consistent with WS-03: user-scope `CLAUDE.md` / settings,
  `--append-system-prompt`, Codex `AGENTS.md` in `CODEX_HOME`, or MCP. No files in the repo.
- Formatting of injected memory: structured, attributed, ordered by precedence, each entry carrying
  its ID so transcripts and Langfuse traces show which entries influenced the run.
- **Memory snapshot pinning**: every job records the exact entry IDs and versions it received
  (in Temporal history by reference and in Langfuse), so a job is reproducible and a bad entry's blast
  radius is queryable.

### 7. Job working memory (`working-memory.md`)

- What state flows between nodes (plan, decisions, constraints discovered, files touched, test
  results) and where each piece lives (Temporal workflow state by value if small, object storage by
  reference if large).
- **CLI session continuity:** sandbox disks are ephemeral, so decide per role whether to (a) capture
  the CLI's session files to object storage and restore them for `--resume` in a later AgentRun, or
  (b) start a fresh session from a structured **handoff document** written at the end of each run.
  Verify where each CLI stores session state. Recommend one approach per role with an ADR; consider
  determinism, size, CLI-version compatibility, and prompt-injection carryover.
- Handoff document schema (goal, plan status, decisions and rationale, open issues, files touched,
  commands that worked, failures to avoid).
- How working memory survives Continue-As-New and repair cycles, and how repair runs receive the
  reviewer's comments, the prior diff, and the prior handoff.

### 8. Context budgeting (`context-budget.md`)

- Budget allocation per role and model context size: system/role prompt, constraints/policy,
  briefing, long-term memory, working memory/handoff, task input, reserved headroom for the agent's
  own work. Give default percentages and absolute caps.
- Enforcement: the Context Assembler node measures token counts (with the target model's tokenizer
  or the gateway's counting endpoint), truncates or summarizes by priority, and records what was cut.
- Oversized briefings go to object storage and are exposed via MCP fetch rather than injected.

### 9. Ecosystem context and briefings (`ecosystem-context.md`)

- Sources and extraction: service catalog derivation, contract extraction (OpenAPI, protobuf,
  GraphQL, AsyncAPI, event schemas) in CI, dependency/call graphs (build graphs, static analysis,
  tracing-derived service graphs from Tempo), CODEOWNERS, lexical/structural code search (Zoekt or
  Sourcegraph) for call sites.
- Storage of the derived graph (Postgres tables vs a graph store; decide), freshness and staleness
  handling.
- Briefing generation: the briefing schema (service summary, contracts touched, consumers, blast
  radius, owners, recent incidents if available, constraints), and when it is generated (per job, at
  context assembly).
- MCP servers exposing catalog, contracts, call sites, and code search to agents; tool definitions.
- CI compatibility checks against extracted contracts (for example `buf breaking`, `oasdiff`), and
  how results feed back into the job.
- Local stubs: fixture catalog, fixture contracts, local Zoekt index over sample repos.

### 10. Evaluation of memory (`memory-evals.md`)

Coordinate with WS-06. Define how the factory proves memory helps: A/B or interleaved evaluation
(jobs with vs without a candidate entry set), metrics (review rounds per PR, repeated-comment rate,
first-pass QA success, reviewer acceptance), offline replay against the golden task set with memory
snapshots, and automatic deprecation of entries correlated with worse outcomes.

### 11. Curation UI requirements (`curation-ui.md`)

All operations below are admin-group only (D-25). Separately, define the read-only memory view that
non-admins get on jobs they can already see, and what it shows or hides (for example provenance
details such as the commenter's identity).

Operations and data the UI needs (WS-07 designs screens): review candidates, approve/reject/edit,
view provenance and usage, resolve conflicts, bulk disable by author/time/scope, see which jobs used an
entry, export.

### 12. PR reply behavior (`pr-replies.md`)

How the factory replies to review comments: acknowledgement, the fix summary with commit link,
when it declines or asks a clarifying question, threading, resolving conversations, and how it
notes that feedback was stored as memory (with a link to the entry).

## Must answer

- A reviewer leaves a bad comment that becomes an active repo rule. How is it detected, and how fast
  can it stop affecting new jobs?
- Two scopes give contradicting rules. What does the agent see?
- How large can the active memory for one repo grow before retrieval quality drops, and what keeps it
  below that?
- How does a job from 3 weeks ago get reproduced with the exact memory it saw?

## Acceptance criteria

- Real DDL, MCP tool definitions, and handoff schema.
- A complete lifecycle state machine for memory entries (Mermaid `stateDiagram-v2`).
- Every write path has an authorization and poisoning control.
- Memory effectiveness is measurable with defined metrics and an automated deprecation rule.
