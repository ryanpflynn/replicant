# WS-11: Local Development and Test Strategy

**Output directory:** `spec/11-local-dev/`
**Constraints most relevant:** D-20, and every constraint that has a local counterpart

## Purpose

Make the whole factory runnable and testable on a laptop before any cloud spend, exercising the same
code paths as production, and define the test strategy from unit tests to full end-to-end jobs.

## Required outputs

### 1. Local stack (`stack.md`)

- A complete `compose.yaml` (Docker Compose and Podman Compose compatible) with profiles:
  - `core`: Temporal (dev server or full server with Postgres; decide), factory Postgres with
    `pgvector`, MinIO, workers, Runner (local Docker/Podman implementation), control-plane UI and API,
    stub IdP (Dex or Keycloak), gateway stub, GitHub mock or a local Git server (Gitea or Forgejo, with
    webhook and PR support; decide and verify the API surface needed), Slack mock, ecosystem stubs
    (catalog, contracts, Zoekt index over sample repos), egress proxy/mirror.
  - `observability`: Langfuse OSS with its dependencies, Grafana, Loki, Tempo, Prometheus, Alloy.
  - `k8s`: a `kind` or `k3d` cluster running the real AgentRun CRD and controller, with Kata where the
    host supports KVM.
- A one-shot `migrate` service that applies Alembic migrations before any factory service starts
  (`depends_on: condition: service_completed_successfully`), per D-26. No walkthrough step may
  involve connecting to Postgres directly.
- One command to start (`make up` or a small `factory dev up` CLI), health checks, seeded data
  loaded through the API with `factory dev seed` (tenant, users, groups, sample repos, definitions,
  a Release Bundle), a `factory dev reset` that rebuilds the database from migrations, and
  resource requirements for
  the laptop. Document macOS (Apple Silicon) and Linux differences honestly, including container VM
  sizing.

### 2. Walkthroughs (`walkthroughs.md`)

Step-by-step, with the exact commands and what the developer sees:

1. Trigger a bugfix job against a sample repo.
2. Watch Temporal history in the Temporal UI.
3. Watch the native CLI run in the sandbox (logs, transcript).
4. Approve the gate from the UI, then from the Slack mock, then simulate a GitHub review.
5. Inspect the diff, QA artifacts, and headless screenshots in MinIO and the UI.
6. Post a PR review comment on the local Git server and watch the repair loop and memory candidate.
7. Sign in as a seeded admin-group user, curate the memory entry in the UI, and see it injected in
   the next job. Then sign in as a non-admin and confirm the memory tools are absent and their API
   endpoints return 403.
8. Ramp a new Release Bundle and watch pinning behavior with a job waiting at a gate.
9. View the job in Langfuse and Grafana, correlated by `job_id`.
10. Run the same job on the `k8s` profile through the real controller.
11. **Real GitHub, personal org:** register the dev GitHub App from the manifest (WS-07
    `github-app.md`), install it on a personal org with a sample repo, start webhook forwarding, open
    an issue, and watch the full loop: acknowledgement comment on the issue → job → approval → PR
    opened by the app → leave a review comment → repair commit and reply. Include teardown (revoke
    the app's credentials, uninstall).

### 3. Stub vs production-identical inventory (`parity.md`)

A table of every component: local implementation, production implementation, identical code path or
not, and known behavioral differences with risk.

### 4. Gateway stub (`gateway-stub.md`)

Modes: pass-through to a real provider with a developer key; record; deterministic replay from
recorded fixtures for CI; scripted fake responses for unit tests.

### 5. Test strategy (`test-strategy.md`)

- Unit tests (nodes, policies, Context Assembler budgeting, memory ranking).
- Temporal workflow tests with the time-skipping test environment, including 30-day waits,
  reminders, and expiry.
- Replay tests against recorded histories for every worker deployment version (with WS-01).
- Controller tests with envtest; e2e on `kind`.
- Contract tests for the Runner API (both implementations pass the same suite).
- End-to-end factory jobs in CI using gateway replay mode.
- Chaos tests: kill workers, controller, Runner, and sandbox nodes mid-job; Postgres failover.
- Security tests: sandbox egress attempts, prompt-injection fixtures, memory-poisoning fixtures.

### 6. Repository layout (`repo-layout.md`)

The factory's own monorepo layout (services, workers, controller, UI, config repo, definitions,
compose, Helm charts, tests), build tooling, and CI pipeline outline.

## Must answer

- What is the minimum laptop spec for the `core` and `observability` profiles together?
- Which production behaviors cannot be reproduced locally, and how are they covered instead?

## Acceptance criteria

- The compose file is complete and valid (`docker compose config` passes in principle; no
  placeholder services).
- Every walkthrough step has exact commands and expected outcomes.
- The Runner API contract test suite is specified so both runner implementations must pass it.
