# replicant-entrypoint

WS-03 runner entrypoint (cli-invocation.md §4): `prepare` (inputs through agentd :7803 with digest checks, blobless
checkout of `base_sha` through agentd :7802 → repo-mirror, prior diff, managed settings + managed MCP, tools copy;
exits 40–43), `run` (bootstrap steps, `$HOME` + joined prompt, `claude -p` stream-json with the §2.1 argv / §2.2 env,
event parsing, turn budget, steering, finalize message, SIGTERM/cancel handling, `kill(-1)`, diff bundle, handoff,
review report / plan, `run_result` validated against the contract + role schemas, `outputs.json`, exit codes §4.6;
role `qa` runs `replicant-qa run --inputs /replicant/inputs --out /replicant/out/qa --plan /replicant/inputs/run-plan.json`
and maps its exit class to 0/44/45), `hook instructions-loaded|guard-paths|session-end`, `materialize`, `version`.
Shipped in `localhost/replicant/cli-claude-code:dev` at `/opt/replicant/bin/replicant-entrypoint`.

Repo configuration (design §8c, `docs/design/onboarding-interfaces.md` §5–6). With run-plan `repo_config: use`:

- **Base-branch config.** `prepare` restores `.claude/**`, every `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md` and
  `.mcp.json` from `config_base_sha`, fetching it if it's missing. It removes config files that aren't at the base and
  makes the rest read-only.
- **CLI flags and env.** `run` passes `--setting-sources user,project --include-hook-events` and drops
  `CLAUDE_CODE_DISABLE_CLAUDE_MDS`.
- **Managed settings.** These set `allowManagedHooksOnly:false`, keep the managed hooks, and drop the `/workspace/**`
  `claudeMdExcludes` entry. They pin the gateway, model and telemetry env. Permission rules and `.mcp.json` stay
  platform-only, except for `mcp_auto_allow` servers, which are added to `allowedMcpServers` (by command) and to
  `managed-mcp.json`.
- **Quoting.** Only `AGENTS.md` and the copilot instructions are quoted, because CLAUDE.md is loaded natively.
- **Report.** The role result's `cli.repo_config` records:
  - the hooks' runs, failures and blocks, with the cause of the last failure, attributed from the CLI's
    `hook_started`/`hook_response` events (the managed hooks print `{"suppressOutput":true}` so they can be told
    apart);
  - the rules, `claude_md` and skills that were loaded;
  - `mcp_skipped` and `mcp_allowed`;
  - `permission_rules_ignored`;
  - `settings_hash`.

  `cli.instruction_files_loaded` comes from the InstructionsLoaded reports kept by agentd
  (`GET /v1/report/instructions`).
- **`repo_config` absent or `ignore`.** Rendering is unchanged.

`agent.model.parameters.reasoningEffort` (`low|medium|high|xhigh|max`) is rendered as `--effort`.

- Test: `podman run --rm -v "$PWD":/src -w /src/go/cmd/replicant-entrypoint -e GOWORK=off docker.io/library/golang:1.26 go test ./...`
