# replicant-entrypoint

WS-03 runner entrypoint (cli-invocation.md §4): `prepare` (inputs through agentd :7803 with digest checks, blobless
checkout of `base_sha` through agentd :7802 → repo-mirror, prior diff, managed settings + managed MCP, tools copy;
exits 40–43), `run` (bootstrap steps, `$HOME` + joined prompt, `claude -p` stream-json with the §2.1 argv / §2.2 env,
event parsing, turn budget, steering, finalize message, SIGTERM/cancel handling, `kill(-1)`, diff bundle, handoff,
review report / plan, `run_result` validated against the contract + role schemas, `outputs.json`, exit codes §4.6;
role `qa` runs `replicant-qa run --inputs /replicant/inputs --out /replicant/out/qa --plan /replicant/inputs/run-plan.json`
and maps its exit class to 0/44/45), `hook instructions-loaded|guard-paths|session-end`, `materialize`, `version`.
Shipped in `localhost/replicant/cli-claude-code:dev` at `/opt/replicant/bin/replicant-entrypoint`.

- Test: `podman run --rm -v "$PWD":/src -w /src/go/cmd/replicant-entrypoint -e GOWORK=off docker.io/library/golang:1.26 go test ./...`
