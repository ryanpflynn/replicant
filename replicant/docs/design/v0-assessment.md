# Replicant v0: engineering assessment

**Date:** 2026-10-26, at the milestone where the notifications and graph-view work was finished.

**Method:** three independent, read-only reviews, each covering different code:
1. The orchestration layer (Temporal and LangGraph).
2. The test suite. All unit tests were run, and the worker scenarios were run partially.
3. Code quality and maintainability of everything else.

The orchestrator then re-checked each finding that drives a recommendation against the code. Nothing was refactored.
The companion document is [v0-system-design.md](v0-system-design.md).

## 1. Verdict

The system is sound and maintainable. The core design choices hold up:
- Temporal as the durable job engine.
- Sandboxed agent runs behind per-run tokens.
- A human approving the exact diff.
- The append-only projection.

The code is better than its build history suggests:
- Permission checks are applied consistently, with a check at startup that every route has one.
- SQL is parameterised.
- The sandbox network boundary is real: a sandbox can reach only the gateway, the mirror, S3, telemetry and the
  runner. An agent therefore cannot approve its own gate.
- About 175 Python, Go and web unit tests pass in about a minute, with 0 failures.

The debt falls into four themes, and none of it blocks the product:

1. **Nothing runs the tests automatically.**
   - There is no CI, no single test command, and no commits yet.
   - The worker's most valuable tests (44 workflow scenarios) are not collected by `pytest` at all.
2. **Spec fidelity costs complexity.**
   - The enterprise spec's shape is implemented where v0 doesn't need it: LangGraph, a 69-service compose file,
     unused DDL, and 12 stub activities.
   - That costs reading time more than it causes bugs.
3. **Plumbing is duplicated.** The same helpers appear 6–8 times across services, there are two Gitea clients and two
   JCS implementations, and contract schemas are vendored 3–4 times.
4. **The local stack is fragile.** podman-compose workarounds, unpruned images and leftover sandbox containers cost
   the most actual time this week, because the VM hung when the disk filled.

**Update 2026-10-27: the real-Claude E2E passed** (design doc §10.1). Before that, the biggest gap was not code quality. **The real-Claude path had never run end to end.** Every E2E so far used the
scripted model backend, so the core value is not yet proven under this machinery.

## 2. Scorecard

| Area | Rating | Basis |
|---|---|---|
| Architecture | **Strong** | Durable workflow, clear trust boundaries, run tokens confined to agentd, idempotent publish, projection reconverges after chaos (X0-7) |
| Security posture | **Good for v0** | Sandbox egress allowlist verified (SEC-EGRESS 10/10), HMAC and signed webhooks, secrets never printed. Gaps in §3 and §6 |
| Correctness | **Good** | 0 unit test failures; E2E-01..04 pass; scenarios 30/44 passed with 0 failed before the run was stopped. One confirmed idempotency bug (§3) |
| Test effectiveness | **Fair** | Good assertions where tests exist, but the most important suites aren't wired in, 4 scenarios report PASS when skipped, and there is no RLS suite |
| Maintainability | **Fair** | Small, cohesive libraries and Go modules. One 2,161-line workflow file, duplicated helpers, and inconsistent auth idioms outside control-plane-api |
| Operability (local) | **Fair–weak** | `dev up` works, but 22 containers, 15 GB of images (87% reclaimable) and podman-compose cascades make it fragile |

## 3. Fix now (small, high value)

These are bugs or near-bugs. Each is small (S).

| # | Finding | Evidence | Fix |
|---|---|---|---|
| F1 | **Fixed 2026-10-27 (FIX-D1).** **Gate-decision idempotency key is too broad.** It is built from the channel and the client's key only, with no gate or person in it, so a reused key returns another gate's outcome and silently drops the new decision | `services/control-plane-api/.../routes_gates.py:134` (`derived("dec", channel, idem)`); `outbox.py:40-55` never checks that the stored row matches the request | Put `gate_id` and `principal_id` in the key; return 409 when the stored row's workflow, kind or actor differ. Same for job-control `action_id` |
| F2 | **Fixed 2026-10-27 (FIX-D2).** **Python logs are not redacted at source.** `replicant_telemetry.JsonLogFormatter` redacts, but none of the 8 Python processes use it; each hand-rolls its own formatter. Redaction happens only in Alloy, which runs only with the observability profile. The Go slog redaction handler that `redact.py` mentions doesn't exist | `libs/python/replicant-telemetry/.../core.py:219`; `control_plane_api/cli.py:28`; `replicant_worker/cli.py:52` | Use one shared formatter everywhere |
| F3 | **Fixed 2026-10-26.** **4 of 44 scenarios report PASS when they were skipped.** `mid_run_can` and the 3 fast-retry outage scenarios return "skipped (run with …)", and the runner counts that as a pass | `workers/replicant-worker/tests/test_scenarios.py:594,1340,1364,1391` | Report SKIP; set the flags in the standard run |
| F4 | **Fixed 2026-10-27 (FIX-D3).** **`--with-observability` never makes X0-8 mandatory.** `E2E_REQUIRE_X08=$X08 true` sets the variable only for the `true` command | `tests/run_m3_pass.sh:10` | Export the variable before `run_clean.sh` |
| F5 | **Moot since 2026-10-26: LangGraph was removed (§4.1).** **LangGraph runs inside the workflow with no sandbox checks**, and nothing turns its tracing off. Setting `LANGSMITH_TRACING` would make network calls from workflow code. Only a comment stops a synchronous router from running on a thread | `adapters/langgraph/pregel.py:70`; `langgraph` and `langchain_core` on the sandbox passthrough list | Force tracing off in `build_config`; assert at compile time that nodes and routers are coroutines. Moot if the engine is dropped (§4.1) |
| F6 | **Fixed 2026-10-27 (FIX-D4).** **A runner test hook is on in the default stack** | `dev/compose/compose.v0.yaml:105` `RUNNER_TEST_HOOKS: "true"` | Turn it on only in the contract-test run |
| F7 | **The design doc overstates tenant isolation.** control-plane-api reads use `OPERATOR_SCOPE` (34 references), which bypasses RLS, so isolation for API reads rests on the application-level `check()` | `grep OPERATOR_SCOPE services/control-plane-api/src` | Corrected in the design doc §9. Enforcement is deferred (§5) |

Lower priority, from the code review:
- **repo-mirror** chooses its upstream from `X-Replicant-Repo-*` headers instead of the grant claims
  (`go/cmd/repo-mirror/main.go:195`). agentd strips those headers, which mitigates it.
- **Notification preferences:** `PUT /v1/me/notification-preferences` ignores `version`, so concurrent edits overwrite
  each other.
- **Repo settings:** `put_repo_settings` forwards the caller's own `Authorization` header to github-integration.

## 4. Simplification opportunities

### 4.1 Temporal + LangGraph: keep Temporal, LangGraph is optional

> **Decided and done on 2026-10-26 (ORCH-D1).** The user chose to remove LangGraph now. Changes:
> - **Engine:** the interpreter is the only engine, and Temporal history is the single state store.
> - **Removed:** the adapter, the code generator, the engine comparison and the engine setting; 21 packages left the
>   lockfile.
> - **Image:** the worker image went from 332 to 296 MB.
> - **Verification:**
>   - Unit tests pass.
>   - Scenarios `bugfix_full`, `cancel_during_run`, `eval_mode`, `repair_budget`, `migration` and `mid_run_can` pass
>     with replay checks.
>   - E2E-01 passed on the new build (`job_01m5wactjc23085haqhtxzt937`, PR #24 merged), and E2E-04 passed.
> - **F5 and the dual-engine test cost no longer apply.** The analysis below is kept as the rationale.

| | Temporal | LangGraph |
|---|---|---|
| What it does here | Durability, replay, timers, Updates (gates), Signals (PR events), Continue-As-New, versioned workers | Steps through a sequential DAG; `interrupt`/`resume` hands control back to the host's wait logic |
| Who owns gates, loops, maxVisits, routing, CAN, node events | — | **Replicant's host code** (`replicant_job.py`), engine-independent |
| Size | — | 132-line adapter, plus `langgraph==1.2.17` and about 15 transitive packages (langchain-core, langsmith, …) |
| Alternative already in the repo | — | `InterpreterEngine`, 79 lines, same `GraphSpec`, passes the same scenarios |

- **What LangGraph adds.**
  - LangGraph state holds only `start_at`, `terminal` and `trail`.
  - There are no reducers carrying data, no `Send`, and no durable checkpointer.
  - The graph UI reads timeline events, not LangGraph state.
  - The v0 `bugfix` graph has no fan-out. Parallel branches exist only in the feature and hotfix shapes.
  - `temporalio-langgraph`, the preview plugin flagged in known-issues §4, **is not a dependency**. ADR-01-001 chose to
    run LangGraph in the workflow instead.
- **Recommendation:** run interpreter-only. **Defer**: it works today, and dropping LangGraph is a deviation from frozen
  decision D-01.
  - **Effort:** about one day, plus a full scenario pass.
  - **Removes:**
    - the adapter;
    - `tools/graph_codegen.py` and `compare_engines.py`;
    - the LangGraph dependency tree;
    - the sandbox passthrough entries;
    - half the scenario run time.
  - **Loses:** only the "LangGraph defines shapes" story.
  - **Trigger:** the first LangGraph upgrade, a determinism or sandbox incident, or the dual-engine tests blocking a
    change.
  - **Until then:** apply F5.
- **Not recommended:** going LangGraph-only. It saves 79 lines and removes the fallback.

### 4.2 Other simplifications

| Opportunity | Value | Effort | When |
|---|---|---|---|
| **Standalone v0 compose** (~400 lines) instead of layering on the 1,656-line, 69-service spec compose, `compose.v0.deps.yaml` and the CLI's second pass. A duplicate key there already dropped config once (NOTIFY-D7) | High: removes the most fragile part of the dev loop | M | Now or next |
| **`replicant dev prune`** (leftover sandbox containers, dangling images, `fstrim`) and fail `dev up` when an image build fails (it currently starts a partial stack) | High: disk-full VM hangs were the top time sink | S | Now |
| **Consolidate duplicated helpers**: `read_secret` (6 copies), JSON log formatter (8), two Gitea clients, two JCS implementations, gateway-shim's own `jose`/`ids` packages, three identity-resolution rules | Medium: fewer places for fixes to diverge (F2 is an instance) | S–M | With F2 |
| **Contract drift test**: byte-compare the schemas vendored in `contracts/`, `go/internal/runner/schemas`, control-plane-api and the worker | Medium | S | Now |
| **Drop `gateway-state` Valkey** (the shim ignores it, GATEWAY-D1); move slack-mock and slack-integration to an opt-in profile | Low–medium: fewer containers | S | Now |
| **One `locked_loop` helper** for the 3 advisory-lock loops in control-plane-api. Today, if a loop's lock connection drops, the loop keeps running without the lock | Medium: correctness under a DB blip | S | Now |
| **Remove dead guards**: 14 `has_relation` checks and the speculative `cost.job_rollups` branch | Low | S | Anytime |
| **Build images from `uv.lock`** (`uv export --frozen`) on one shared Python base. Today transitive dependencies float, so images differ from the tested environment | Medium: reproducibility | S–M | Now |
| **Split `replicant_job.py`** (2,161 lines, 127 methods) along its existing `# ====` sections | Medium | M | Defer: the next change to waits or Updates |
| **Make `notify_*` its own package** inside control-plane-api; split it into a separate service only when needed | Medium | S now, M later | Defer the split: a second replica or real Slack volume |
| **Unused spec surface**: 12 `NotImplementedInV0` activities; DDL for memory, evals, ecosystem and jira (4.4k lines). They fail fast, so the cost is reading time | Low | — | Keep; mark v0-dead paths in the worker README |

**Keep. These are load-bearing:**
- The token broker as its own process. It is the only holder of SCM admin credentials.
- agentd and run tokens.
- The `replicant-sbx` network and sbx-edge.
- STS per-run policies.
- Schema validation at boundaries.
- runner-local's pod derivation, which the future k8s runner shares.
- The dev-auth package split.

## 5. Tests

### 5.1 What exists and passes

| Suite | Result |
|---|---|
| Python unit tests, 15 packages | about 175 pass, 0 fail, 12 skipped (need a DB), about 1 minute in total |
| Go, 8 modules | all pass, about 14 s. No tests in sandbox-sidecars or the runner `engine`, `sandbox`, `s3x`, `revoke` and `ipc` packages |
| Web | vitest 14 pass; Playwright 4 graph tests pass; type check clean |
| Worker scenarios, 44 on 2 engines | stopped at 30/44 PASS per engine, 0 FAIL, after 26.5 min. Not run by `pytest` |
| E2E-01..04, chaos (CH-WRK/RUN/PG-01), SEC-EGRESS | pass; E2E-01 takes about 93 s per job; a full M3 pass takes 15–20 min |

**Strongest tests:**
- Publisher idempotency against a real Gitea.
- Projector convergence with shuffled and duplicated events on real Postgres.
- Scenario `common_checks`: replay, contract validation, and duplicate-ID checks.

### 5.2 Gaps, by risk

1. **Nothing runs the tests automatically.**
   - No CI, no single command, and 15 separate `uv run` invocations plus Go and Node containers. Host Node is v16, so
     vitest runs only in a container.
   - The worker's `pyproject.toml` sets `python_files` to just two files, so the scenarios and `test_units.py` are
     manual scripts.
2. **No RLS or tenant-isolation suite.**
   - The WS-10 "0 tables without RLS" gate ran on the spec, but no test in the repo asserts it, or that cross-tenant
     API reads return 404.
   - This matters more given F7.
3. **DB-gated tests are skipped by default.** The 12 skipped tests are the ones covering webhook auth, gate-decision
   outbox and intake pinning. The projector already shows the pattern for starting Postgres in a session fixture.
4. **No negative E2E for separation of duties.** It's covered at unit and scenario level, but no test has the requester
   try to approve over HTTP and get 403.
5. **E2E drivers depend on shared stack state.**
   - `e2e01` doesn't reset the sample repo.
   - The reset helper lives in the chaos script and is reached through `sys.path` hacks.
   - The notify E2Es depend on in-memory overrides that a control-plane-api restart clears.
   - `e2e03` has a bare `sleep(35)`.
   - Both reviewers' scenario runs killed each other's processes: shared, long-running suites with no isolation.
6. **Only part of the runner contract runs.** The driver defines about 70 RC-* cases, but the standard pass runs only
   SEC-EGRESS. RC-RUN-13 is open.

### 5.3 Simplifications

- **E2E code.** Move the E2E helpers into one `replicant_e2e` module and turn the drivers into pytest tests with a
  repo-reset fixture.
- **Runner scripts.** Fold `run_clean.sh` and `run_m3_pass.sh` into `replicant dev test {unit,e2e,m3}`.
- **Engine scenarios.** Run the full scenario set on one engine. Run the second engine plus `compare_engines` on a
  subset only when `graphs/engines/` changes, and fix `compare_engines`'s `PARALLEL_NODES`, which hides qa/review
  ordering on the bugfix graph. Dropping LangGraph (§4.1) removes this cost entirely.

## 6. Recommended order

**Now. About 3–4 days in total, no architectural change:**
1. F1, F2, F4 and F6 (F3 is fixed and F5 is moot).
2. `replicant dev test unit`: every package, Go and vitest in containers, DB tests on via a session fixture, and the
   scenarios as pytest with fast and slow markers.
3. An RLS and tenant-isolation suite, plus the separation-of-duties negative E2E.
4. `replicant dev prune`, a fatal build failure, and image builds from `uv.lock`.
5. **Run the real-Claude passthrough E2E.** It needs `ANTHROPIC_API_KEY` in `dev/.env`. It is the only way to prove
   the product's core value, and it may surface issues the scripted backend hides.

**Next. Before more features:**
6. Standalone v0 compose.
7. Consolidate the duplicated helpers, the contract drift test, and `locked_loop`.
8. Commit the repo and add CI: unit tests on every PR, scenarios plus scripted E2E nightly.

**Deferred, with triggers:**

| Item | Trigger |
|---|---|
| Split `replicant_job.py` | Next change to waits or Updates |
| Separate notify service | Second control-plane-api replica, or real Slack volume |
| RLS-enforced API reads (`tenant=` on reads) | Real identity (Dex) or a second real tenant |
| Full RC-* contract suite, RC-CAN-01 hook | Work on X0-4 |
| Authz and prompt-injection security suites (spec §8.2–8.5) | Real identity |
| Config consolidation (about 250 `REPLICANT_*` names; generated env reference) | When onboarding a second developer |
