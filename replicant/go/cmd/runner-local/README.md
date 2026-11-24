# runner-local

Runner API (C-RUNNER 4.0.0) for the compose profile (spec/02-execution/local-runner.md): admission (A-1…A-15 as
applicable locally), idempotent `createRun` (`spec_digest` excludes `run_scope`/`trace_context`), the run-scope check
against core's intake record (`GET /internal/v1/jobs/{job_id}/run-scope` with `RUNNER_CORE_TOKEN_FILE`; 422
`run_scope_invalid`, SIMPLIFY-D50), per-audience run tokens signed with its own key (JWKS on both listeners), get/list/cancel, steering inbox, sandbox listener (token refresh, S3
credentials, inbox, heartbeat/lease), the revocation feed (`GET /v1/revocations` + ops bucket; it also carries every
local run of a job core revokes, polled from core's `GET /internal/v1/revocations`), kill-switch mirror,
pod-group executor over the Docker/Podman API (the controller's `Derive` state machine, copied from spec/02-execution),
and the in-process notifier (Temporal Signal `run.status_changed`). Deviations: docs/v0/DEVIATIONS.md RUNNER-D*.

- Build: `podman build -t localhost/replicant/replicant-go:dev -f images/replicant-go/Dockerfile .` (repo root; the shared Go image, SIMPLIFY-D2).
- Test: `podman run --rm -v "$PWD":/src -w /src/go/cmd/runner-local -e GOWORK=off docker.io/library/golang:1.26 go test ./...`
- Run: compose service `runner-local` (v0 overlay sets socket, state volume, sandbox network); listeners :8480
  (orchestration) and :8490 (sandbox, JWKS). `runner-local healthcheck --url http://127.0.0.1:8480/readyz`.
- Smoke + contract cases (isolated `run-` stack: SeaweedFS, Temporal dev server, the real gateway shim in script mode,
  runner-local, and contract fakes for core (run scope, revocations)/Gitea; the driver stages each run's repo as a git bundle in its inputs,
  as the worker does, SIMPLIFY-D40):
  `go/cmd/runner-local/smoke/run-smoke.sh [--build] [--keep] [--only SMOKE-IMPL]`. Results in
  `~/.cache/replicant-runner-smoke/results.json`. `--only RC-REPO-CFG` is the §8c repo-configuration case. It commits
  `.claude/` config, a nested CLAUDE.md and `.mcp.json` on a branch of the fake Gitea repo, and runs with
  `repo_config: use`. It then checks five things:
  - the repo hook ran;
  - the rules and nested CLAUDE.md were loaded;
  - the URL MCP server was skipped;
  - the managed `Bash(curl:*)` deny beat the project's allow rule;
  - the config paths stayed out of the diff.

  It needs a `cli-claude-code` image with the current entrypoint and agentd.
