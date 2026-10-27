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
uv run --package replicant-e2e python tests/e2e/e2e01_v0.py   # E2E: Gitea issue labelled `replicant` -> merged PR
uv run --package replicant-e2e python tests/e2e/e2e02_notify.py   # notifications: comments, DMs, inbox, reminder
uv run --package replicant-e2e python tests/e2e/e2e03_slack.py    # Slack buttons, channel threads, digest, App Home
tests/run_m3_pass.sh --no-build               # runner contract (egress) and chaos passes
uv run replicant dev down -v                  # stop and remove volumes
```

Web UI: http://127.0.0.1:8400 ("Waiting on you" at /waiting). Slack mock: http://127.0.0.1:8410. Gitea: http://127.0.0.1:3300. Temporal UI: http://127.0.0.1:8233 (namespace
`replicant-local`). Demo helper: `uv run --package replicant-e2e python tests/e2e/demo.py start`.

## Layout

| Path | What |
|---|---|
| `cli/` | `replicant` CLI (`replicant dev up/down/status/seed/logs`) |
| `services/` | control-plane-api, github-integration, github-token-broker, slack-integration |
| `tools/slack-mock/` | local Slack Web API mock (records messages; viewer on :8410) |
| `workers/` | replicant-worker (Temporal `ReplicantJob`), publisher-worker, replicant-projector |
| `libs/python/` | replicant-ids, replicant-db, replicant-temporal, replicant-telemetry, replicant-cdef, replicant-scm |
| `go/` | runner-local, repo-mirror, replicant-entrypoint, replicant-agentd, replicant-qa, replicant-gateway-shim |
| `web/control-plane-web/` | management UI |
| `dev/` | compose files (project `replicant`), Postgres image, sample repo |
| `tests/` | E2E, runner contract, chaos |

Python is a uv workspace (3.12). Go is one `go.work` (Go 1.26), built and tested in `docker.io/library/golang:1.26`.
