# Replicant

Replicant, a software factory: it turns a labelled issue into a planned, implemented, QA-checked and reviewed pull
request, with a human approving the exact diff before anything is published.

This is the monorepo. The specification is in `../spec` (start at `spec/README.md`); the frozen contracts are in
`spec/00-contracts` and are copied byte for byte into `contracts/`. The local v0 plan is `docs/v0/PLAN.md`; deviations
and cross-component decisions are in `docs/v0/DEVIATIONS.md` and `docs/v0/INTERFACES-LOG.md`. The system design, including
work not yet completed, is `docs/design/v0-system-design.md`.

## Local stack (Podman)

```sh
uv sync --all-packages
uv run replicant dev up --profile v0          # generate secrets, build localhost/replicant/* images, start, wait healthy
uv run replicant dev seed                     # dev bundle, tenants, Gitea repo and webhook
uv run replicant dev status
uv run replicant dev test e2e                 # E2E drivers + Playwright against the running stack (gateway: script)
uv run replicant dev down -v                  # stop and remove volumes
```

## Tests

```sh
uv run replicant dev test                     # unit (default): every Python package, Go modules, web tsc + vitest; one table
uv run replicant dev test unit --only control-plane-api,go,web:vitest --fail-fast --junit out/junit
uv run replicant dev test unit --slow         # + all 44 worker scenarios (about 25 min; default runs the fast five)
uv run replicant dev test e2e                 # e2e01, e2e04, e2e02, e2e03, e2e05, then Playwright (live_gate make/finish)
uv run replicant dev test m3 --yes            # tests/run_m3_pass.sh: down -v, E2E-01, SEC-EGRESS, chaos (destructive)
make test | make test-e2e | make prune        # the same, ARGS="..." for options
```

- **unit** runs suites in parallel (`-j 4`). Go runs in `docker.io/library/golang:1.26` with the `plat-gomod` and
  `plat-gocache` volumes; web runs in `docker.io/library/node:22` (host Node is 16).
- DB-gated tests (control-plane-api `test_db_integration.py`, github-integration) run by default against
  `services/control-plane-api/tests/smoke/env.sh` (Postgres on :55432). It is started for the run and torn down after,
  unless `--keep-db`, or unless it was already running. `--no-db` skips them. The `migrate` image is rebuilt
  automatically when `libs/python/replicant-db` changed after it was built (`--build` forces it).
- Worker scenarios (`workers/replicant-worker/tests/test_scenarios.py`) are pytest tests on a session-scoped Temporal
  dev server; all but five are marked `slow`. With `--slow`, the scenarios gated on `REPLICANT_EXTRA_NODE_MODULES`,
  `REPLICANT_HISTORY_CAN_EVENTS=150` or `REPLICANT_TEST_FAST_RETRY=1` run in extra pytest invocations with that variable.
- **lint:python** is `ruff check` (config in the root `pyproject.toml`); **lint:go** is `go vet ./...` per Go module.
- The E2Es and `tests/runner-contract/run-v0.sh` switch the gateway to `script` for the run and restore it after.
  `uv run replicant dev gateway-mode [script|passthrough]` prints or switches it; `dev up` keeps it unless `--gateway`.
- Logs and JUnit XML of the last run are in `.test-results/`. The E2E drivers share helpers in `tests/e2e/replicant_e2e`
  (installed by `uv sync --all-packages`) and reset the sample repo to the planted bug before each job.

## Disk

```sh
uv run replicant dev prune                    # finished sandbox containers, exited one-shots, dangling images, fstrim
uv run replicant dev prune --images --volumes # + unused images (keeps replicant :dev, compose, base and test images)
                                              #   and anonymous / finished-run volumes; asks first unless --yes
```

The Podman VM hangs when the host disk fills; below 10 GiB free, `replicant dev up` and `dev test e2e|m3` run
`dev prune --volumes` on their own first. Named volumes of project
`replicant` (Postgres, S3, Gitea, ...) are never removed by `prune`.

Web UI: http://127.0.0.1:8400 ("Waiting on you" at /waiting). Slack mock: http://127.0.0.1:8410. Gitea: http://127.0.0.1:3300. Temporal UI: http://127.0.0.1:8233 (namespace
`replicant-local`). Demo helper: `uv run --package replicant-e2e python tests/e2e/demo.py start`.

## Layout

| Path | What |
|---|---|
| `cli/` | `replicant` CLI (`replicant dev up/down/status/seed/logs/test/prune`) |
| `services/` | control-plane-api, github-integration, github-token-broker, slack-integration |
| `tools/slack-mock/` | local Slack Web API mock (records messages; viewer on :8410) |
| `workers/` | replicant-worker (Temporal `ReplicantJob`), publisher-worker, replicant-projector |
| `libs/python/` | replicant-ids, replicant-db, replicant-temporal, replicant-telemetry, replicant-cdef, replicant-scm |
| `go/` | runner-local, repo-mirror, replicant-entrypoint, replicant-agentd, replicant-qa, replicant-gateway-shim |
| `web/control-plane-web/` | management UI |
| `dev/` | compose files (project `replicant`), Postgres image, sample repo |
| `tests/` | E2E, runner contract, chaos |

Python is a uv workspace (3.12). Go is one `go.work` (Go 1.26), built and tested in `docker.io/library/golang:1.26`.
