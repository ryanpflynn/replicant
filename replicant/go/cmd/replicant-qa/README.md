# replicant-qa (minimal v0 harness)

`replicant-qa run --inputs /replicant/inputs --out /replicant/out/qa --plan /replicant/inputs/run-plan.json`: stages `install`
(`go mod download`), `unit` (`go test -json ./...`, JUnit), `build` (`go build ./...`); writes `result.json`
(`{verdict, exit_class, role_result}` with `replicant.role-result.qa/v1`), `outputs.entries.json`, build logs, JUnit, a
summary `qa_report`, and checkpoints in `/replicant/out/qa-checkpoints/`. Exit classes per the R1V-023 ruling (install
failure → `environment_setup`). Shipped in the CLI image at `/opt/replicant/bin/replicant-qa`.

When the run plan has `environment.commands` (design §8c, `docs/design/onboarding-interfaces.md` §5), the repo's own
commands run instead, through the shell, in the order `install`, `build`, `unit` (the `test` command) and `lint`;
unconfigured ones are not listed. Each failed stage gets a `cause` and `detail`:

- `missing_tool` (exit 127, "command not found", `uv`/`pnpm` spawn errors; `detail` is the binary) and `network`
  (DNS or connection errors): the environment's fault, so the verdict is `inconclusive`/`environment_failure`;
- `timeout`, or the stage's own `install_failure`, `build_failure`, `test_failure` or `lint_failure`.

A failed install is always `inconclusive`; lint failures are warnings (`pass_with_warnings`). Test counts come from
pytest, jest or vitest summary lines when present.

- Test: `podman run --rm -v "$PWD":/src -w /src/go/cmd/replicant-qa -e GOWORK=off docker.io/library/golang:1.26 go test ./...`
