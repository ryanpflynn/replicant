# CLI Invocation and the Sandbox Runner Entrypoint

**Workstream:** WS-03 · **Constraints:** D-10, D-11, D-12, D-14, D-17, D-21 · **Contracts consumed:** C-RUNNER 3.0.0,
C-CRD 3.0.0, C-ART 2.0.0, C-IDS 2.0.0, C-DEF 3.0.0

## Purpose

Fix exactly how Replicant runs native coding CLIs (Claude Code and Codex) headless inside an AgentRun, with nothing
committed to the target repository: which processes run in the pod, which files exist before the CLI starts and what
writes them, the exact argv, environment, and config files per CLI, how prompts, memory, and steering reach the agent,
how transcripts, session state, and results leave it, and the exit-code and result-file contract the controller and
workflow rely on.

## Decisions summary

| # | Decision | Constraint | ADR |
|---|---|---|---|
| 1 | Two WS-03 processes per pod: **`replicant-entrypoint`** (agent container, holds no credentials, launches the CLI) and **`replicant-agentd`** (native sidecar, sole holder of the run token, loopback auth proxy for gateway/MCP/git/inputs, token refresh, steering long-poll). The agent never sees a credential | D-21 | [ADR-001](adr/001-entrypoint-and-agentd-credential-isolation.md) |
| 2 | Both are static Go binaries shipped **inside the pinned CLI image**, so the CLI adapter (flags, config rendering, event parsing) is versioned and pinned with the CLI digest. A CLI upgrade, including a flag change, stays a configuration change (CC-1) | D-10, D-03 | [ADR-001](adr/001-entrypoint-and-agentd-credential-isolation.md), [ADR-004](adr/004-cli-delivery-into-toolchain-pod.md) |
| 3 | Claude Code CLI (`claude -p`, stream-json in and out) for every Claude role. The Agent SDK is not used inside sandboxes in v1; `cli: claude-agent-sdk` stays reserved | D-10 | [ADR-002](adr/002-claude-code-cli-over-agent-sdk.md) |
| 4 | Codex runs as `codex exec --json` with `--sandbox danger-full-access` (the AgentRun is the sandbox). App-server (live steering) is experimental upstream and not allowed in promotable bundles | D-10, D-06 | [ADR-003](adr/003-codex-exec-over-app-server.md) |
| 5 | Security-relevant CLI configuration lives in **read-only mounts** written before the agent container starts (Claude Code managed settings and managed MCP under `/etc/claude-code`; Codex security keys as `-c` overrides, the highest precedence layer). Mutable user-scope files in `$HOME` carry only conveniences | D-10, D-21 | this doc §1.4 |
| 6 | Target-repo instruction files (`CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/`, `.mcp.json`) are **not auto-loaded** by default; the context assembler may quote them as untrusted context | D-10, D-21 | [ADR-007](adr/007-repo-instruction-files.md) |
| 7 | All model traffic goes CLI → agentd (loopback) → Replicant gateway shim → enterprise gateway. CLIs carry a non-secret placeholder credential | D-11 | [ADR-006](adr/006-loopback-auth-proxy.md), [gateway.md](gateway.md) |
| 8 | Structured results use the CLIs' own schema enforcement (`--json-schema`, `--output-schema`) for the agent-produced part; the entrypoint computes everything else (diff stats, usage, integrity) from trusted observation | D-21 | this doc §4.7 |
| 9 | Exit 0 means "valid `run_result` written". Every non-zero code maps 1:1 to a C-CRD failure reason | C-CRD §4 | this doc §4.6 |
| 10 | Inputs have one writer: WS-04's `context.stage_inputs` writes `inputs/manifest.json`; WS-03's `agents.build_run_plan` writes `inputs/run-plan.json` and hands its entry over; the entrypoint consumes composed `system-append.md` and `first-message.md` | C-ART §4, C-RUNNER `RunSpec.inputs` | §4.4; XD-FIX-R1-067 |
| 11 | One session-resume rule, SR-1 (same cycle, identical CLI image digest, < 24 h, ≤ 256 MiB, tree match), owned here and cited by WS-04 and WS-06 | D-14 | §2.8; XD-FIX-R2-053 |
| 12 | The run-level turn budget is enforced by the entrypoint across steering; `--max-turns` is a per-message backstop | D-11 | §2.6 (R2-043) |
| 13 | Role `qa` runs WS-05's `replicant-qa` harness from the `qa-runner` CLI image; QA artifact types flow through the same outputs index | D-16 | §4.10 (R1-073) |

```mermaid
flowchart LR
  subgraph POD["AgentRun pod ar-… (RuntimeClass replicant-kata-clh / replicant-gvisor)"]
    direction LR
    subgraph INIT["init phase"]
      PREP["prepare (init container, CLI image, uid 10001)<br/>replicant-entrypoint prepare"]
    end
    subgraph SIDE["native sidecars (restartPolicy: Always)"]
      AGD["replicant-agentd (CLI image, uid 10000)<br/>run token · loopback :7800-7803 · steering"]
      UPL["uploader / shipper / watchdog (WS-02)"]
      SVC["services e.g. postgres (env def)"]
    end
    subgraph MAIN["agent container (environment image, uid 10001)"]
      EP["replicant-entrypoint run (PID 1)"]
      CLI["claude -p … | codex exec --json …"]
      EP -->|"stdin stream-json / argv"| CLI
      CLI -->|"stdout events"| EP
    end
  end
  TOK[("Secret ar-…-creds<br/>(mounted ONLY in agentd)")] --> AGD
  CLI -->|"http://127.0.0.1:7800 (no secret)"| AGD
  PREP -->|"inputs, git via :7802/:7803"| AGD
  EP <-->|"unix socket /run/replicant/ipc/agentd.sock"| AGD
  AGD -->|"Bearer run token + X-Replicant-*"| GW["replicant-gateway-shim → enterprise AI gateway"]
  AGD -->|"Bearer run token"| MCP["memory-mcp / context-mcp"]
  AGD -->|"Bearer run token"| MIR["repo-mirror"]
  AGD -->|"/sandbox/v1/*"| RSL["runner-sandbox listener"]
  EP -->|"/replicant/out + /replicant/spool"| UPL --> S3[("S3 run prefixes")]
```

---

## 0. Verified facts this document depends on

All checked on 2026-10-24 against the pinned versions **Claude Code 2.1.310** and **Codex CLI 0.164.0**. The CLI
image build re-runs the adapter contract tests (§7) against the exact binary, so a fact that changes in a later
version fails CI instead of failing runs.

| ID | Fact | Source | Status |
|---|---|---|---|
| F-CC-1 | Latest Claude Code is 2.1.310; native per-platform packages exist for `linux-x64`, `linux-x64-musl`, `linux-arm64`, `linux-arm64-musl` | https://registry.npmjs.org/@anthropic-ai/claude-code/latest ; https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64-musl/latest | [verified: https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64-musl/latest, 2026-10-24] |
| F-CC-2 | `-p --output-format stream-json` requires `--verbose`; `--input-format stream-json` requires `--output-format stream-json` | binary error strings in 2.1.310 | [verified: https://registry.npmjs.org/@anthropic-ai/claude-code-darwin-arm64/-/claude-code-darwin-arm64-2.1.310.tgz (string in shipped binary), 2026-10-24] |
| F-CC-3 | Flags `--max-turns`, `--max-budget-usd` (print mode, client-side estimate, may overshoot), `--model`, `--fallback-model` (comma list), `--tools`, `--allowedTools`, `--disallowedTools`, `--permission-mode` (`default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`), `--mcp-config`, `--strict-mcp-config`, `--append-system-prompt-file`, `--system-prompt-file`, `--settings`, `--setting-sources user,project,local`, `--resume`, `--session-id` (UUID), `--fork-session`, `--json-schema`, `--include-partial-messages`, `--replay-user-messages` | https://code.claude.com/docs/en/cli-reference.md | [verified: https://code.claude.com/docs/en/cli-reference.md, 2026-10-24] |
| F-CC-4 | Since 2.1.302, `-p` with no permission mode starts in Manual mode and denies calls needing approval. `dontAsk` "auto-denies every tool call that would otherwise prompt", intended "for CI pipelines or restricted environments where you pre-define what Claude may do". Deny rules block in every mode. Managed `permissions.disableBypassPermissionsMode: "disable"` and `permissions.disableAutoMode: "disable"` exist | https://code.claude.com/docs/en/permission-modes.md | [verified: https://code.claude.com/docs/en/permission-modes.md, 2026-10-24] |
| F-CC-5 | Settings precedence, highest first: managed, command line, project local, shared project, user. Linux managed file `/etc/claude-code/managed-settings.json` plus drop-ins `/etc/claude-code/managed-settings.d/*.json`. Managed settings always apply regardless of `--setting-sources` | https://code.claude.com/docs/en/settings.md ; https://code.claude.com/docs/en/managed-settings.md | [verified: https://code.claude.com/docs/en/settings.md, 2026-10-24] |
| F-CC-6 | Memory files: managed `/etc/claude-code/CLAUDE.md`, `~/.claude/CLAUDE.md`, project and ancestor `CLAUDE.md`/`CLAUDE.local.md`, `.claude/rules/`; repo `AGENTS.md` read when no `CLAUDE.md` exists (2.1.277+). `pluginConfigs["cc-plugin-agents-md@builtin"].options.instructionFiles: "managed-only"` drops project, local, and user CLAUDE.md and every AGENTS.md, keeping managed CLAUDE.md; honored from managed settings. `claudeMdExcludes` takes absolute-path globs. Project rules are skipped when `project` is excluded from `--setting-sources` | https://code.claude.com/docs/en/memory.md | [verified: https://code.claude.com/docs/en/memory.md, 2026-10-24] |
| F-CC-7 | In `-p`, project `.mcp.json` servers load without a prompt; mitigations are `--strict-mcp-config`, excluding `project` from `--setting-sources`, `disabledMcpjsonServers` | https://code.claude.com/docs/en/mcp.md | [verified: https://code.claude.com/docs/en/mcp.md, 2026-10-24] |
| F-CC-17 | `--system-prompt-snapshot off` rebuilds the system prompt on each request instead of reusing the one recorded at the conversation's start (2.1.257+); `CLAUDE_CODE_PROJECT_DIR_NAME` names the `projects/` directory when `CLAUDE_CONFIG_DIR` is set (2.1.234+, read only from the start environment); `CLAUDE_CODE_RETRY_WATCHDOG=1` retries 429/529 indefinitely (opt-in) | https://code.claude.com/docs/en/cli-reference.md ; https://code.claude.com/docs/en/env-vars.md | [verified: https://code.claude.com/docs/en/env-vars.md, 2026-10-24] |
| F-CC-16 | `/etc/claude-code/managed-mcp.json` (same format as `.mcp.json`) gives exclusive control: only its servers (plus `managedMcpServers`) load; with it deployed, `--mcp-config` or `--strict-mcp-config` make Claude Code exit at startup. `allowManagedMcpServersOnly: true` + `allowedMcpServers: [{"serverUrl": …}]` makes the managed allowlist authoritative. `allowManagedPermissionRulesOnly` (managed only) makes managed settings the only source of allow/ask/deny rules and ignores `--allowedTools` | https://code.claude.com/docs/en/managed-mcp.md ; https://code.claude.com/docs/en/settings-reference.md | [verified: https://code.claude.com/docs/en/managed-mcp.md, 2026-10-24] |
| F-CC-8 | Env: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` (sent as `Authorization: Bearer`), `ANTHROPIC_CUSTOM_HEADERS` (`Name: Value`, newline separated), `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, `MAX_THINKING_TOKENS`, `API_TIMEOUT_MS`, `CLAUDE_CODE_MAX_RETRIES` (default 10, cap 15), `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_AUTOUPDATER`, `DISABLE_UPDATES`, `DISABLE_ERROR_REPORTING`, `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, `CLAUDE_CODE_DISABLE_WEB_FETCH` (2.1.285+), `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`, `BASH_DEFAULT_TIMEOUT_MS`, `BASH_MAX_TIMEOUT_MS`, `BASH_MAX_OUTPUT_LENGTH`, `MCP_TIMEOUT`, `MCP_TOOL_TIMEOUT`, `MAX_MCP_OUTPUT_TOKENS`, `CLAUDE_CODE_SHELL`, `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default 600000), `CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_BEDROCK_BASE_URL`, `CLAUDE_CODE_SKIP_BEDROCK_AUTH`, `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_BASE_URL`, `CLAUDE_CODE_SKIP_VERTEX_AUTH`, `ANTHROPIC_VERTEX_PROJECT_ID` | https://code.claude.com/docs/en/env-vars.md | [verified: https://code.claude.com/docs/en/env-vars.md, 2026-10-24] |
| F-CC-9 | OTel: `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER`, `OTEL_TRACES_EXPORTER` (needs `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`), `OTEL_EXPORTER_OTLP_PROTOCOL` (no default), `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_LOGS_EXPORT_INTERVAL`, `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`, `OTEL_RESOURCE_ATTRIBUTES`. `traceparent` is sent to a custom `ANTHROPIC_BASE_URL` only with `CLAUDE_CODE_PROPAGATE_TRACEPARENT=1`; inbound `TRACEPARENT`/`TRACESTATE` env is honored in `-p` | https://code.claude.com/docs/en/monitoring-usage.md | [verified: https://code.claude.com/docs/en/monitoring-usage.md, 2026-10-24] |
| F-CC-10 | stream-json `result` subtypes: `success`, `error_max_turns`, `error_during_execution`, `error_max_budget_usd`, `error_max_structured_output_retries`; fields include `session_id`, `num_turns`, `total_cost_usd`, `usage`, `modelUsage`, `permission_denials`, `structured_output`, `stop_reason`. `system/init` carries `session_id`, `tools`, `mcp_servers[{name,status}]`, `model`, `permissionMode`, `apiKeySource`, `claude_code_version`, `cwd`. Exit code 0 on success, non-zero on failure; SIGTERM gives exit 143 | https://code.claude.com/docs/en/headless.md ; @anthropic-ai/claude-agent-sdk 0.3.310 `sdk.d.ts` | [verified: https://code.claude.com/docs/en/headless.md, 2026-10-24] |
| F-CC-11 | stream-json input line: `{"type":"user","message":{"role":"user","content":…},"parent_tool_use_id":null}`; a message queued after a turn starts a new turn; closing stdin ends input | @anthropic-ai/claude-agent-sdk 0.3.310 `sdk.d.ts` (SDKUserMessage); https://code.claude.com/docs/en/headless.md | [verified: https://code.claude.com/docs/en/headless.md, 2026-10-24] (shape from shipped SDK types) |
| F-CC-12 | Hooks: events include `PreToolUse`, `PostToolUse`, `InstructionsLoaded`, `SessionStart`, `SessionEnd`, `Stop`; `type: command`; `timeout` seconds; exit 2 blocks; `allowManagedHooksOnly` (managed) | https://code.claude.com/docs/en/hooks.md | [verified: https://code.claude.com/docs/en/hooks.md, 2026-10-24] |
| F-CC-13 | Sessions stored at `$CLAUDE_CONFIG_DIR/projects/<cwd with non-alphanumerics replaced by '-'>/<session-id>.jsonl`; `--resume <id>` finds a session in any project on the machine; `cleanupPeriodDays` default 30 | https://code.claude.com/docs/en/sessions.md ; https://code.claude.com/docs/en/claude-directory.md | [verified: https://code.claude.com/docs/en/sessions.md, 2026-10-24] |
| F-CC-15 | Built-in tool names include `Agent`, `Bash`, `Edit`, `Write`, `Read`, `Glob`, `Grep` (absent by default on Linux), `NotebookEdit`, `TaskCreate/Get/List/Update`, `EnterWorktree`, `Artifact`, `PushNotification`, `RemoteTrigger`, `SendMessage`, `SendUserFile`, `Skill`, `Workflow`, `WebFetch`, `WebSearch`; permission rules support `//` absolute paths and `mcp__<server>__*` | https://code.claude.com/docs/en/tools-reference.md ; https://code.claude.com/docs/en/permissions.md | [verified: https://code.claude.com/docs/en/permissions.md, 2026-10-24] |
| F-CC-14 | WebSearch is an Anthropic server-side tool and is not exposed by Bedrock; disable with `--disallowedTools WebSearch` | https://code.claude.com/docs/en/tools-reference.md | [verified: https://code.claude.com/docs/en/tools-reference.md, 2026-10-24] |
| F-CX-1 | Latest Codex stable is 0.164.0; the `linux-x64` npm package ships the static `x86_64-unknown-linux-musl` binary plus bundled `rg` and `bwrap` | https://registry.npmjs.org/@openai/codex/-/codex-0.164.0-linux-x64.tgz | [verified: https://registry.npmjs.org/@openai/codex/-/codex-0.164.0-linux-x64.tgz, 2026-10-24] |
| F-CX-2 | `codex exec` flags `--json`, `--output-last-message/-o`, `--output-schema`, `--model/-m`, `--sandbox/-s {read-only,workspace-write,danger-full-access}`, `--skip-git-repo-check`, `--cd/-C`, `--add-dir`, `-c key=value` (TOML-parsed), `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, `--strict-config`, prompt from stdin with `-`; `codex exec resume <SESSION_ID>`. Exec has no `-a`: approval is hard-coded `never`. `--full-auto` was removed | https://learn.chatgpt.com/docs/non-interactive-mode ; https://learn.chatgpt.com/docs/developer-commands?surface=cli ; openai/codex `codex-rs/exec/src/lib.rs` @ rust-v0.164.0 | [verified: https://learn.chatgpt.com/docs/non-interactive-mode, 2026-10-24] |
| F-CX-3 | `--json` events: `thread.started{thread_id}`, `turn.started`, `turn.completed{usage{input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens}}`, `turn.failed{error}`, `item.started/updated/completed` with item types `agent_message`, `reasoning`, `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, `todo_list`, `error`; exit 1 on failed or interrupted turn | https://learn.chatgpt.com/docs/non-interactive-mode ; `codex-rs/exec/src/exec_events.rs` @ rust-v0.164.0 | [verified: https://learn.chatgpt.com/docs/non-interactive-mode, 2026-10-24] |
| F-CX-4 | Config precedence: CLI flags and `-c` > project `.codex/config.toml` (trusted projects only) > profile file > `$CODEX_HOME/config.toml` > cloud defaults > `/etc/codex/config.toml` > built-ins. `[profiles.*]` tables are no longer read; `--profile x` layers `$CODEX_HOME/x.config.toml`. Untrusted projects skip project-scoped `.codex/` layers | https://learn.chatgpt.com/docs/config-file/config-basic ; https://learn.chatgpt.com/docs/config-file/config-advanced | [verified: https://learn.chatgpt.com/docs/config-file/config-basic, 2026-10-24] |
| F-CX-5 | `model_providers.<id>` keys `name`, `base_url`, `env_key`, `wire_api` (only `responses`; `chat` is now a config error), `http_headers`, `env_http_headers`, `request_max_retries` (4), `stream_max_retries` (5), `stream_idle_timeout_ms` (300000), `auth.command`. With neither `env_key` nor `requires_openai_auth`, Codex sends no auth | https://learn.chatgpt.com/docs/config-file/config-reference ; https://learn.chatgpt.com/docs/auth | [verified: https://learn.chatgpt.com/docs/config-file/config-reference, 2026-10-24] |
| F-CX-6 | AGENTS.md: global `$CODEX_HOME/AGENTS.override.md` or `AGENTS.md`; project files from project root to cwd, capped by `project_doc_max_bytes` (32 KiB). `project_doc_max_bytes = 0` returns no project doc; `trust_level = "untrusted"` skips the project scan | https://learn.chatgpt.com/docs/agent-configuration/agents-md ; `codex-rs/core/src/agents_md.rs` @ rust-v0.164.0 | [verified: https://learn.chatgpt.com/docs/agent-configuration/agents-md, 2026-10-24] (disable behavior from source) |
| F-CX-7 | Linux sandbox is `bwrap` + seccomp and needs unprivileged user namespaces; docs: "If the container is your intended security boundary, run Codex with `--sandbox danger-full-access` inside the container" | https://learn.chatgpt.com/docs/agent-approvals-security ; https://learn.chatgpt.com/docs/sandboxing | [verified: https://learn.chatgpt.com/docs/agent-approvals-security, 2026-10-24] |
| F-CX-8 | `shell_environment_policy.inherit` defaults to `all` and `ignore_default_excludes` defaults to `true`, so tool subprocesses inherit secrets unless filtered | https://learn.chatgpt.com/docs/config-file/config-reference ; `codex-rs/protocol/src/config_types.rs` | [verified: https://learn.chatgpt.com/docs/config-file/config-reference, 2026-10-24] |
| F-CX-9 | `[otel]` `exporter`/`trace_exporter`/`metrics_exporter` (`otlp-http`, `otlp-grpc`), `environment`, `log_user_prompt` (false); the HTTP client injects W3C `traceparent` when a trace exporter is configured; `TRACEPARENT` env sets the parent | https://learn.chatgpt.com/docs/config-file/config-advanced ; `codex-rs/http-client/src/client.rs` | [verified: https://learn.chatgpt.com/docs/config-file/config-advanced, 2026-10-24] (traceparent from source) |
| F-CX-10 | Rollouts at `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`; `history.persistence = "none"` disables `history.jsonl` | `codex-rs/rollout/src/recorder.rs` @ rust-v0.164.0 ; https://learn.chatgpt.com/docs/config-file/config-advanced | [verified: https://learn.chatgpt.com/docs/config-file/config-advanced, 2026-10-24] |
| F-CX-11 | `developer_instructions` injects additional developer instructions; `model_instructions_file` replaces built-in instructions; top-level `web_search` = `disabled` | https://learn.chatgpt.com/docs/config-file/config-reference | [verified: https://learn.chatgpt.com/docs/config-file/config-reference, 2026-10-24] |
| F-CX-12 | app-server `turn/steer` appends input to the active turn, but "The app-server command and WebSocket transport are experimental and aren't supported for production workloads" | https://learn.chatgpt.com/docs/app-server | [verified: https://learn.chatgpt.com/docs/app-server, 2026-10-24] |
| F-K8S-1 | Termination messages truncated at 4096 bytes; 12 KiB total across all containers divided equally | https://kubernetes.io/docs/tasks/debug/debug-application/determine-reason-pod-failure/ | [verified: https://kubernetes.io/docs/tasks/debug/debug-application/determine-reason-pod-failure/, 2026-10-24] |
| F-K8S-2 | Image volumes (`volumes[].image`) stable from Kubernetes 1.36; runtime must support them | https://kubernetes.io/docs/concepts/storage/volumes/#image | [verified: https://kubernetes.io/docs/concepts/storage/volumes/#image, 2026-10-24] |

Unverified items this document touches, each with a fallback, are listed in §9.

---

## 1. Pod process and filesystem model

### 1.1 Containers (WS-02's pod, adopted per XD-02-01)

WS-02 owns the pod template (C-CRD §6; spec/02-execution/controller.md §4.2). WS-03 adopts WS-02's shape,
including the two differences WS-02 recorded in XD-02-01 (R1-072): the uploader is a **regular** container that
outlives the agent container, and WS-02's trusted containers run as uid **10003**, so no WS-02 component can read
agentd's private tmpfs.

| Order | Container | Kind | Image | uid | Command | Mounts relevant to WS-03 |
|---|---|---|---|---|---|---|
| 1 | `workspace-guard` | init | `replicant/sandbox-utils` (WS-02) | 10001 | WS-02 (fail-closed hard-limit check, exit 84) | `workspace` |
| 2 | `replicant-agentd` | native sidecar, `startupProbe` on `/readyz` | CLI image (by digest) | **10000** | `/opt/replicant/bin/replicant-agentd` | `run-tokens` (Secret `ar-…-creds`, one file per audience, `defaultMode: 0400`) **only here**, ro at `/run/replicant/token-seed` (`REPLICANT_RUN_TOKEN_SEED_DIR`); `agentd-state` (tmpfs) at `/run/replicant/tokens` (`REPLICANT_RUN_TOKEN_DIR`, agentd's live token copies, §5.1; C-IDS 2.0.0 §8); `ipc`; `uploader-ipc`; `config` |
| 3 | `watchdog` | native sidecar | `replicant/sandbox-utils` | 10003 | WS-02 | `workspace` (ro), `ipc` |
| 4 | env services (`svc-<name>`) | native sidecars; `startupProbe` from `readiness` **except for role `qa`** (XD-05-14, XD-02-15) | service image (by digest) | image default, non-root | from the definition | data under `/workspace/.services/<name>` (no separate emptyDir, XD-02-15) |
| 5 | `prepare` | init | CLI image | 10001 | `/opt/replicant/bin/replicant-entrypoint prepare` | `tools`, `inputs`, `managed`, `home`, `workspace`, `ipc`, `config` |
| 6 | `agent` | main (`terminationMessagePath: /dev/termination-log`) | **environment image** `spec.image.resolved.image` | 10001 | `/opt/replicant/bin/replicant-entrypoint run` | `tools` **ro** at `/opt/replicant`; `managed` **ro** at `/etc/claude-code` and `/etc/replicant`; `inputs` **ro** at `/replicant/inputs`; `home` at `/home/agent`; `workspace` at `/workspace`; `out` at `/replicant/out`; `spool` at `/replicant/spool`; `ipc` at `/run/replicant/ipc`; `prompt` (tmpfs) at `/run/replicant/prompt` (joined prompt, §1.4, XD-W2-03-01); `podinfo` (ro) |
| 7 | `uploader` | **regular** container (outlives `agent`) | `replicant/sandbox-uploader` (WS-02) | 10003 | WS-02 | `out` (ro), `spool` (ro), `ipc`, `uploader-ipc`, `config` |

QA runs add WS-05's `stub-<name>` and optional `builder` sidecars (spec/05-qa/stage-design.md §4.1); the `agent`
container is unchanged and runs `replicant-entrypoint run` with `REPLICANT_ROLE=qa` (§4.10).

Pod-level: `fsGroup: 10002` (`replicant-io`), `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, capabilities
`drop: [ALL]`, `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false` (C-CRD §6),
`shareProcessNamespace: false` (the agent cannot see or signal sidecar processes).

Volumes (all `emptyDir` except `run-tokens`, `config`, `podinfo`, and `workspace`):

| Volume | Backing | Size | Contents |
|---|---|---|---|
| `workspace` | AgentRun `spec.workspace` (enforcement per WS-02, contracts ADR-014) | `workspace.sizeLimit` | `/workspace/repo` (checkout), `/workspace/context/<name>` (additional read-only repos), `/workspace/.cache/*` (workspace caches), `/workspace/.services/<name>` (service data), `/workspace/.qa/**` (QA) |
| `home` | emptyDir | 2 GiB | `/home/agent` (CLI user config, CLI session files, package manager config) |
| `tools` | emptyDir (image volume later, [ADR-004](adr/004-cli-delivery-into-toolchain-pod.md)) | 1 GiB | `/opt/replicant/bin/{replicant-entrypoint,replicant-agentd,replicant-qa,replicant-svc-wrap,replicant-stubgen}`, `/opt/replicant/cli/<cli>/<version>/…`, `/opt/replicant/etc/gitconfig` |
| `managed` | emptyDir | 16 MiB | `/etc/claude-code/managed-settings.json`, `/etc/claude-code/managed-mcp.json`, `/etc/replicant/codex-overrides.toml` |
| `inputs` | emptyDir | 2 GiB | `/replicant/inputs/manifest.json`, `run-plan.json`, `system-append.md`, `memory-injection.json`, `first-message.md`, `context/*`, `qa/*` |
| `prompt` | emptyDir (`medium: Memory`) | 4 MiB | `/run/replicant/prompt/system-append.md`: the joined system prompt (role, constraints, memory block). `agent` container only; never uploaded, never captured (XD-W2-04-01; volume requested from WS-02 in XD-W2-03-01) |
| `out` | emptyDir | `limits.max_output_bytes` | `/replicant/out/**` (outputs index, run_result, diff bundle, handoff, session capture, archive, `qa/**`) |
| `spool` | emptyDir | 2 GiB | `/replicant/spool/{cli_events,transcript,sandbox_log}/part-*.ndjson`, `/replicant/spool/control/cli-invocation.json` |
| `ipc` | emptyDir (`medium: Memory`) | 1 MiB | `/run/replicant/ipc/agentd.sock`, `/run/replicant/ipc/status/*` |
| `uploader-ipc` | emptyDir (`medium: Memory`) | 64 KiB | `/run/replicant/uploader-ipc/agentd.sock` (agentd ↔ uploader only, **not** mounted in `agent`; `REPLICANT_UPLOADER_SOCKET`) |
| `run-tokens` | projected Secret `ar-<body>-creds` (`defaultMode: 0400`) | — | `/run/replicant/token-seed/<audience>` (`REPLICANT_RUN_TOKEN_SEED_DIR`), mounted read-only in `replicant-agentd` only; read once at start |
| `agentd-state` | emptyDir (`medium: Memory`) | 1 MiB | `/run/replicant/tokens/<audience>` (`REPLICANT_RUN_TOKEN_DIR`): agentd's live, refreshed token copies, mode 0400 uid 10000; `replicant-agentd` only (C-IDS 2.0.0 §8) |

### 1.2 Trust model inside the pod

- **Untrusted**: everything in the `agent` container after the first bootstrap step starts (bootstrap runs repo
  scripts such as `postinstall`), the CLI, the model, and any process the agent spawns. They run as uid 10001 and
  can read and write `home`, `workspace`, `out`, `spool`.
- **Trusted**: `replicant-agentd` (uid 10000, separate container, no shared PID namespace), the WS-02 sidecars, and the
  `prepare` init container (it runs before any repo-controlled code executes).
- `replicant-entrypoint run` shares uid 10001 with the agent. It is trusted only at two points: **before** it executes the
  first bootstrap step, and **after** it has killed every other process in the container (`kill(-1, SIGKILL)` from
  PID 1, §4.5). Anything it reads between those points (CLI events, hook reports) is treated as untrusted data and
  only used for reporting. Security decisions come from agentd (usage, budget) and from the post-kill workspace scan.
- Read-only mounts (`/etc/claude-code`, `/etc/replicant`, `/replicant/inputs`, `/opt/replicant`) cannot be changed by the
  agent, so the security-relevant CLI configuration holds for the whole run even though `$HOME` is writable.

### 1.3 Ports and sockets

| Endpoint | Owner | Served to | Forwards to (token audience) |
|---|---|---|---|
| `http://127.0.0.1:7800/anthropic/…`, `/openai/…`, `/bedrock/…`, `/vertex/…` | agentd | CLI (loopback, shared pod network namespace) | replicant-gateway-shim (`ai-gateway`); every route of gateway.md GW-1 has a loopback route (R1-080) |
| `http://127.0.0.1:7801/mcp/{memory,context,code_search}` | agentd | CLI MCP clients | memory-mcp (`memory-mcp`), context-mcp (`context-mcp`) |
| `http://127.0.0.1:7802/git/<repo_id>` | agentd | `prepare` (fetch) and, after prepare, **read-only lazy blob fetches** of the blobless checkout (`git-upload-pack` only, repos in the grant's `repo_ids`, ≤ 20 requests/min) | repo-mirror (`repo-mirror`) |
| `http://127.0.0.1:7803/inputs/<name>` | agentd, **closed once `prepare` reports done** | `prepare` | S3 `inputs/` via run-scoped STS (`runner-sandbox` → `GET /sandbox/v1/credentials/aws`) |
| `unix:/run/replicant/ipc/agentd.sock` | agentd | entrypoint (and, unavoidably, the agent) | IPC API §5.3; exposes no credential |
| `unix:/run/replicant/uploader-ipc/agentd.sock` | agentd | uploader only (socket not mounted in `agent`) | `GET /v1/aws-credentials` (§5.3, XD-02-01) |

agentd binds only 127.0.0.1. Under Kata both are inside the guest network namespace; under gVisor inside the Sentry
netstack. No pod ingress exists (contracts ADR-010 "no inbound connections" is about the network; loopback never
leaves the pod). The Runner-set proxy and mirror environment (`HTTPS_PROXY`, `NO_PROXY`, package mirror variables in
`bootstrap.json`) reaches bootstrap steps, the CLI, and every agent shell: the entrypoint copies it into the process
environment after the protected-name filter, which only blocks *definitions* from setting those names (R1-094,
XD-11-09). `NO_PROXY` always contains `127.0.0.1,localhost` so loopback routes never go through a proxy.

### 1.4 Must-answer: exactly which files exist in the sandbox home directory when the CLI starts, and what writes them

`HOME=/home/agent`. Nothing below is written into `/workspace/repo` (D-10). "prepare" means `replicant-entrypoint
prepare` (init container, before any repo code runs); "run" means `replicant-entrypoint run` immediately before it
spawns the CLI (after bootstrap, so a bootstrap script that tampered with `$HOME` is overwritten). These paths are the
single layout for all workstreams (XD-FIX-R1-068; WS-04 adopts them).

**Inputs (all CLIs; `inputs` volume, read-only to the agent):**

| Path | Staged by | Content |
|---|---|---|
| `/replicant/inputs/manifest.json` | `context.stage_inputs` (WS-04), the **single** manifest writer (XD-FIX-R1-067) | `replicant.run-inputs/v1` |
| `/replicant/inputs/run-plan.json` | `agents.build_run_plan` (WS-03) | `replicant.run-plan/v1`: agent spec, effective limits, rendered prompt sources, workspace and environment steps |
| `/replicant/inputs/system-append.md` | `context.stage_inputs` (`place: system_append`) | role prompt + "\n\n" + Replicant constraints, **no memory text** (XD-W2-04-01) |
| `/replicant/inputs/memory-injection.json` | `context.stage_inputs` (`place: system_append_memory`, type `memory_injection`, `replicant.memory-injection/v1`) | the memory block; the **only** staged copy of memory text, so a memory purge deletes it (C-ART rule 5, XD-FIX-R1-105) |
| `/run/replicant/prompt/system-append.md` (tmpfs) | run | the joined prompt: `system-append.md` bytes + "\n\n" + `memory-injection.json` `.rendered` (just `system-append.md` when the block is empty), after verifying both sha256s from the manifest; mode 0444; the file every CLI reads (§2.1, §3.1). Never uploaded: it is outside `/replicant/out` and `/replicant/spool`, and the uploader does not mount `prompt` |
| `/replicant/inputs/first-message.md` | `context.stage_inputs` | first user turn: task, handoff section, reviewer feedback, steering backlog |
| `/replicant/inputs/context/<path>` | `context.stage_inputs` | task packet, plan, briefing, quoted repo instruction files, attachments (`place: context_file`, R1-089); the memory block is **not** here (it is `memory-injection.json`, above) |
| `/replicant/inputs/qa/<path>` | `context.stage_inputs` / QA staging | QA contracts, `plan-acceptance.json` (`place: qa_file`, WS-05) |

**Claude Code runs** (`CLAUDE_CONFIG_DIR=/home/agent/.claude`, `CLAUDE_CODE_PROJECT_DIR_NAME=replicant-workspace`):

| Path | Written by | Mode | Content |
|---|---|---|---|
| `/etc/claude-code/managed-settings.json` | prepare (into `managed`, mounted ro) | 0444 | Permission mode, allow/deny rules, disabled bypass/auto modes, managed-only hooks/permission rules/MCP, instruction-file policy, env pins (§2.3) |
| `/etc/claude-code/managed-mcp.json` | prepare | 0444 | Loopback MCP routes (§2.5); exclusive MCP control |
| `/home/agent/.claude/settings.json` | run (rewritten) | 0644 | User scope: `cleanupPeriodDays`, `autoMemoryEnabled: false`, plus `claudeCode.extraSettings` after the adapter's key filter (§2.4) |
| `/home/agent/.claude/projects/replicant-workspace/<session-id>.jsonl` (+ the session's side directory) | prepare, only when the session-resume rule SR-1 admits a restore (§2.8) | 0644 | Prior session |
| `/home/agent/.claude/.claude.json` | **the CLI itself** at first start (`$CLAUDE_CONFIG_DIR/.claude.json`, §9 U-3) | — | CLI global state. Not pre-written |
| `/home/agent/.replicant/agent-report.schema.json` | run | 0444 | The role schema's `$defs/agent_report`, passed inline to `--json-schema` |
| `/home/agent/.gitconfig` | run | 0644 | `user.name=replicant-agent`, `user.email=replicant-agent@replicant.invalid`, `safe.directory=/workspace/repo`, `core.hooksPath=/dev/null`, `init.defaultBranch=main`, `advice.detachedHead=false`, `credential.helper=` (empty); in deterministic mode also fixed dates via env (§2.2) |
| Package-manager config per `packageMirrors` | run | 0644 | `~/.npmrc` (`registry=`), `~/.config/pip/pip.conf` (`index-url`), `~/.m2/settings.xml` (mirror), `~/.gradle/init.d/mirror.gradle`, `~/.cargo/config.toml` (source replacement); Go uses env (`GOPROXY`, `GOFLAGS`, `GONOSUMDB`, `GOPRIVATE`) from the bootstrap config, whose values follow WS-09/WS-02's R2-003 resolution (curated replicant-controlled mirror). Endpoints are always the curated virtual repositories (environment-definition.md §3.1, control-review G-10) |

There is no `~/.claude/CLAUDE.md`, no managed `CLAUDE.md` (constraints live in the joined prompt, one place), no
`~/.claude/agents/`, `commands/`, `skills/`, `plugins/`, or `rules/`.

**Codex runs** (`CODEX_HOME=/home/agent/.codex`):

| Path | Written by | Mode | Content |
|---|---|---|---|
| `/home/agent/.codex/config.toml` | run (rewritten) | 0644 | Model provider `replicant` (loopback), MCP servers, OTel, `history.persistence = "none"`, `web_search = "disabled"`, `shell_environment_policy`, project trust `untrusted`, `project_doc_max_bytes = 0` (§3.2). Security-relevant keys are **also** passed as `-c` overrides, the highest-precedence layer |
| `/home/agent/.codex/AGENTS.md` | run | 0444 | Symlink to `/run/replicant/prompt/system-append.md` (the joined prompt, tmpfs; global AGENTS.md; XD-W2-04-01). Codex following the symlink is U-13 (§9); the adapter contract test asserts it, and the fallback is a regular 0444 copy in `$CODEX_HOME`, which no capture or archive includes |
| `/home/agent/.codex/sessions/YYYY/MM/DD/rollout-…-<thread_id>.jsonl` | prepare, only on an SR-1 restore | 0644 | Prior rollout |
| `/etc/replicant/codex-overrides.toml` | prepare (ro mount) | 0444 | The canonical `-c` key/value list; kept for audit and the materialization dry-run |
| `/home/agent/.replicant/agent-report.schema.json`, `~/.gitconfig`, package-manager files | run | | as above |

`auth.json` is never created (no `codex login`); the provider sends no `Authorization` header and agentd adds it.

## 2. Claude Code

### 2.1 Invocation (exact argv)

Implementer example (`examples/agent-implementer-claude.yaml`, run budget $15, fresh session):

```bash
exec /opt/replicant/cli/claude-code/2.1.310/claude \
  -p \
  --input-format stream-json \
  --output-format stream-json \
  --verbose \
  --model anthropic/claude-sonnet-5-5 \
  --fallback-model anthropic/claude-opus-5-5 \
  --max-turns 200 \
  --max-budget-usd 15.00 \
  --permission-mode dontAsk \
  --setting-sources user \
  --append-system-prompt-file /run/replicant/prompt/system-append.md \
  --tools Bash,Read,Edit,Write,Agent,TaskCreate,TaskGet,TaskList,TaskUpdate \
  --disallowedTools WebFetch,WebSearch,NotebookEdit,EnterWorktree,Artifact,PushNotification,RemoteTrigger,SendUserFile,SendMessage,Workflow,Skill \
  --json-schema "$(cat /home/agent/.replicant/agent-report.schema.json)" \
  --session-id 6f1d2c9a-5b3e-5c47-9a8d-1e2f3a4b5c6d
```

Resume under the session-resume rule SR-1 (§2.8) replaces the last flag with
`--resume <prior session_id> --fork-session --system-prompt-snapshot off` (the snapshot flag rebuilds the system
prompt from the current joined prompt, whose memory block is possibly revocation-filtered, instead of the recorded one; it exists from
2.1.257 [verified: https://code.claude.com/docs/en/cli-reference.md, 2026-10-24], and the adapter contract test
asserts it, R2-053).

(The entrypoint execs with an argv array, not a shell; the `$(cat …)` above only illustrates that the schema JSON is
passed as the flag value.)

Rendering rules (the adapter in `replicant-entrypoint`, package `adapter/claudecode`):

| Agent definition / plan field | Flag / file | Rule |
|---|---|---|
| `model.primary` | `--model` | Gateway model ID verbatim (current Claude IDs are undated, e.g. `anthropic/claude-sonnet-5-5`, C-DEF §4.3); the shim maps it upstream. Aliases (`sonnet`) are never passed |
| `model.fallbacks` | `--fallback-model` | Comma-joined, omitted if empty |
| `model.small` / `model.subagent` | env `ANTHROPIC_DEFAULT_HAIKU_MODEL` / `CLAUDE_CODE_SUBAGENT_MODEL` | Absent ⇒ set both to `model.primary` so no unlisted model is ever requested (403 `model_not_allowed`) |
| `plan.effective.max_turns` | `--max-turns` (backstop only) + entrypoint run-level turn budget | `--max-turns` applies per user message and resets with each steering or finalize message, so the **run-level** budget is enforced by the entrypoint across the whole run (§2.6, R2-043) |
| `plan.effective.budget_usd` | `--max-budget-usd` | Two decimals. Client-side early stop only; the gateway is the authority |
| `tools.*` | `--permission-mode dontAsk`, managed `permissions.allow/deny`, `--tools`, `--disallowedTools` | §2.3 |
| composed system append | `--append-system-prompt-file /run/replicant/prompt/system-append.md` (mode `append`) or `--system-prompt-file` (mode `replace`) | The joined prompt (§1.4): `context.stage_inputs` stages role + constraints as `system-append.md` and the memory block as `memory-injection.json`; the entrypoint joins them on tmpfs (XD-W2-04-01). Replace is allowed only for `planner` and `reviewer` (lint) |
| composed first message | first stdin message | `/replicant/inputs/first-message.md` (§2.6) |
| `outputContract.structuredResult = cli_schema` | `--json-schema` | The role result schema's `$defs/agent_report`, `$ref`s inlined |
| session (fresh) | `--session-id <uuid5(NAMESPACE_URL, "replicant:" + run_id)>` | Deterministic, so a retried prepare/run of the same `run_id` produces the same session path |
| session (resume) | `--resume <id> --fork-session --system-prompt-snapshot off` | Only when SR-1 admits it (§2.8) |
| `claudeCode.includePartialMessages` | `--include-partial-messages` | Off by default (event volume) |

Flags deliberately **not** used: `--dangerously-skip-permissions` / `bypassPermissions` (allow rules would have no
effect and protected-path checks are skipped, F-CC-4); `auto` (adds classifier model calls through the gateway and
is non-deterministic); `--bare` (it also skips MCP and hooks, which Replicant needs); `--worktree` (writes
`.claude/worktrees/` into the repo); `--continue` (cwd-scoped picker semantics); `--no-session-persistence` on roles
that capture (D-14; planner, reviewer, and QA agent tasks do pass it, per WS-04 ADR-04-001); `--add-dir` (additional
repos are read via file tools inside `/workspace/context`); **`--mcp-config` and `--strict-mcp-config`**: with
`/etc/claude-code/managed-mcp.json` deployed, either flag makes Claude Code exit at startup (F-CC-16), so MCP comes only
from the managed file (settles XD-04-02's request; XD-FIX-R1-068).

### 2.2 Environment

Set by the entrypoint on the CLI process. The controller-provided `REPLICANT_*`, `TRACEPARENT`, `TRACESTATE`,
`OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_ENDPOINT` (`https://otel-sandbox.<env>.replicant.internal:4318`),
`OTEL_EXPORTER_OTLP_CERTIFICATE=/etc/replicant-ca/ca.crt` (C-IDS §7, XD-FIX-R1-041) and the Runner-set proxy and mirror variables
(§1.3) are inherited; `TRACEPARENT` is replaced by the `cli.invocation` span's context (§4.3).

```bash
HOME=/home/agent
CLAUDE_CONFIG_DIR=/home/agent/.claude
CLAUDE_CODE_PROJECT_DIR_NAME=replicant-workspace            # fixed session directory (needs CLAUDE_CONFIG_DIR; 2.1.234+)
CLAUDE_CODE_SHELL=/bin/bash
ANTHROPIC_BASE_URL=http://127.0.0.1:7800/anthropic
ANTHROPIC_AUTH_TOKEN=replicant-loopback-placeholder      # not a secret; agentd strips and replaces it
ANTHROPIC_DEFAULT_HAIKU_MODEL=anthropic/claude-haiku-5-5
CLAUDE_CODE_SUBAGENT_MODEL=anthropic/claude-sonnet-5-5
CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000
MAX_THINKING_TOKENS=8000
API_TIMEOUT_MS=600000                                  # gateway.requestTimeoutSeconds * 1000
CLAUDE_CODE_MAX_RETRIES=4                              # agentd already retried 429/529 (gateway.md section 6)
CLAUDE_CODE_RETRY_WATCHDOG=0                           # explicit: no indefinite 429/529 retries; agentd owns waits and GatewayUnavailable
CLAUDE_CODE_PROPAGATE_TRACEPARENT=1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
DISABLE_AUTOUPDATER=1
DISABLE_UPDATES=1
DISABLE_ERROR_REPORTING=1
CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
CLAUDE_CODE_DISABLE_CLAUDE_MDS=1                       # defense in depth; semantics undocumented (section 9 U-11)
CLAUDE_CODE_DISABLE_WEB_FETCH=1
CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1
CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=60000              # do not idle 10 min after the last turn
BASH_DEFAULT_TIMEOUT_MS=300000
BASH_MAX_TIMEOUT_MS=1800000
BASH_MAX_OUTPUT_LENGTH=30000
MCP_TIMEOUT=30000
MCP_TOOL_TIMEOUT=60000
MAX_MCP_OUTPUT_TOKENS=25000
CLAUDE_CODE_ENABLE_TELEMETRY=1
CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1                  # traces (beta)
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=none                                # XD-08-03: event logs would ship tool output around the redactor (D-21)
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative
OTEL_METRICS_INCLUDE_SESSION_ID=false
OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false
OTEL_METRIC_EXPORT_INTERVAL=30000
OTEL_LOG_USER_PROMPTS=0
OTEL_LOG_TOOL_DETAILS=0
# deterministic mode only (plan.effective.deterministic, XD-11-09):
#   GIT_AUTHOR_DATE / GIT_COMMITTER_DATE = plan.effective.git_identity_date (the job's received_at), TZ=UTC
# plus definition env (agent.env, environment.env), cache env (GOCACHE, npm_config_cache, ...), mirror env
```

The OTel variables are XD-08-03's set [verified: https://code.claude.com/docs/en/monitoring-usage.md, 2026-10-24]
(`OTEL_METRICS_INCLUDE_SESSION_ID`, `OTEL_METRICS_INCLUDE_ACCOUNT_UUID`,
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`).
`CLAUDE_CODE_RETRY_WATCHDOG` is opt-in ("Set to `1` for unattended sessions … Retries `429` and `529` capacity errors
indefinitely") [verified: https://code.claude.com/docs/en/env-vars.md, 2026-10-24]; Replicant sets it to `0`
explicitly so a version that changes the default cannot turn a provider outage into a run that waits until its
deadline instead of failing fast with `GatewayUnavailable` (R2 lens-6).

Bedrock/Vertex-fronting gateways (`gateway.protocol = bedrock_invoke | vertex_raw_predict`) replace the two
`ANTHROPIC_*` endpoint variables with:

```bash
CLAUDE_CODE_USE_BEDROCK=1
ANTHROPIC_BEDROCK_BASE_URL=http://127.0.0.1:7800/bedrock
CLAUDE_CODE_SKIP_BEDROCK_AUTH=1        # agentd/shim sign upstream, no AWS model credentials in the sandbox
AWS_REGION=us-east-1                   # set defensively (section 9 U-4)
# or
CLAUDE_CODE_USE_VERTEX=1
ANTHROPIC_VERTEX_BASE_URL=http://127.0.0.1:7800/vertex     # agentd route → shim /vertex/v1/... (GW-1)
CLAUDE_CODE_SKIP_VERTEX_AUTH=1
ANTHROPIC_VERTEX_PROJECT_ID=replicant    # placeholder, the shim ignores project/location from the sandbox
CLOUD_ML_REGION=global                 # placeholder (semantics not documented, section 9 U-4)
```

The protected-name filter (`common.schema.json#/$defs/protected_env_name`) guarantees definitions cannot override
any variable above.

### 2.3 Managed settings (`/etc/claude-code/managed-settings.json`)

Rendered for the implementer example:

```json
{
  "permissions": {
    "defaultMode": "dontAsk",
    "disableBypassPermissionsMode": "disable",
    "disableAutoMode": "disable",
    "allow": [
      "Read", "Edit", "Write", "Bash", "Agent",
      "TaskCreate", "TaskGet", "TaskList", "TaskUpdate",
      "mcp__memory__search_memory", "mcp__memory__get_memory_entry",
      "mcp__context__*", "mcp__code_search__*"
    ],
    "deny": [
      "WebFetch", "WebSearch", "NotebookEdit", "EnterWorktree", "Artifact", "PushNotification",
      "RemoteTrigger", "SendUserFile", "SendMessage", "Workflow", "Skill",
      "Bash(git push:*)", "Bash(git remote add:*)", "Bash(curl:*)", "Bash(wget:*)",
      "Bash(ssh:*)", "Bash(docker:*)",
      "Read(//run/replicant/**)", "Read(//proc/*/environ)",
      "Edit(//etc/**)", "Edit(//opt/replicant/**)", "Edit(//replicant/inputs/**)"
    ]
  },
  "allowManagedHooksOnly": true,
  "allowManagedPermissionRulesOnly": true,
  "allowManagedMcpServersOnly": true,
  "allowedMcpServers": [{"serverUrl": "http://127.0.0.1:7801/*"}],
  "hooks": {
    "InstructionsLoaded": [
      {"hooks": [{"type": "command", "command": "/opt/replicant/bin/replicant-entrypoint hook instructions-loaded", "timeout": 10}]}
    ],
    "PreToolUse": [
      {"matcher": "Edit|Write|NotebookEdit",
       "hooks": [{"type": "command", "command": "/opt/replicant/bin/replicant-entrypoint hook guard-paths", "timeout": 10}]}
    ],
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "/opt/replicant/bin/replicant-entrypoint hook session-end", "timeout": 10}]}
    ]
  },
  "pluginConfigs": {
    "cc-plugin-agents-md@builtin": {"options": {"instructionFiles": "managed-only"}}
  },
  "claudeMdExcludes": ["/workspace/**", "/home/agent/**"],
  "disabledMcpjsonServers": ["*"],
  "autoMemoryEnabled": false,
  "cleanupPeriodDays": 30,
  "env": {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_WEB_FETCH": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  }
}
```

Rendering of `tools`:

| Field | Rendered as |
|---|---|
| (always) | `--tools` is an explicit built-in allowlist: Claude Code 2.1.310 has many tools that reach Anthropic-hosted services or write into the repo (`Artifact`, `PushNotification`, `RemoteTrigger`, `SendUserFile`, `SendMessage`, `Workflow`, `EnterWorktree` creates `.claude/worktrees/` in the repo) [verified: https://code.claude.com/docs/en/tools-reference.md, 2026-10-24]. Only the tools below are ever listed; the same names are also denied in managed settings so a later CLI version that ignores `--tools` for a new tool still cannot use it. `TaskCreate/TaskGet/TaskList/TaskUpdate` (session checklist) are always listed |
| `read: true` | `--tools` + allow `Read`. On Linux, Claude Code leaves `Glob` and `Grep` out of the default set and searches with embedded `bfs`/`ugrep` through `Bash` [verified: https://code.claude.com/docs/en/tools-reference.md, 2026-10-24]; for agents with `shell.mode: none` the adapter lists `Glob,Grep` explicitly (contract test asserts they load) |
| `edit: true` | `--tools` + allow `Edit`, `Write`. `edit: false` omits them from `--tools` and adds `Edit`, `Write`, `NotebookEdit` to **deny** (deny wins in every mode, F-CC-4) |
| `editPaths` (with `edit: true`) | instead of plain `Edit`/`Write`, one allow rule per glob: `Edit(//workspace/.qa/generated/**)`, `Write(//workspace/.qa/generated/**)`; with `dontAsk`, edits elsewhere are denied (qa-runner) |
| `shell.mode: any` | allow `Bash`; each `shell.deny` pattern `p` becomes deny `Bash(p)` |
| `shell.mode: allowlist` | each `shell.allow` pattern becomes allow `Bash(p)`; plain `Bash` is not allowed, so `dontAsk` denies everything else |
| `shell.mode: none` | deny `Bash` |
| `web.*: false` | deny `WebFetch`, `WebSearch`; env `CLAUDE_CODE_DISABLE_WEB_FETCH=1`; `--disallowedTools` removes them from context |
| `subagents.enabled` | `--tools` + allow `Agent`, or deny `Agent` |
| `mcpServers[].tools.allow` | allow `mcp__<name>__<tool>`; an empty list allows `mcp__<name>__*` (allow rules accept a glob only after a literal `mcp__<server>__` prefix [verified: https://code.claude.com/docs/en/permissions.md, 2026-10-24]); `tools.deny` become deny rules |
| `denyTools` | added to `--disallowedTools` and to deny |

Path-scoped deny rules use the absolute-path `//` form (`//path` = "Absolute path from filesystem root"; a single
leading `/` anchors at the settings source; deny rules also match symlink targets) [verified:
https://code.claude.com/docs/en/permissions.md, 2026-10-24]. Bash prefix rules use the trailing `:*` form, which is
equivalent to a trailing ` *` [verified: same page]. They are defense in depth only:
the token is not mounted in the agent container at all (ADR-001), `/etc/**`, `/opt/replicant/**`, `/replicant/inputs/**`
are read-only mounts, and the diff collector strips forbidden paths (§4.8).

The `guard-paths` hook reads the PreToolUse JSON from stdin and exits 2 (block, F-CC-12) when `tool_input.file_path`
resolves (after symlink resolution) outside `/workspace/` or into a D-10 agent-config path
(`CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `AGENTS.override.md`, `.claude/**`, `.codex/**`, `.mcp.json`,
`.cursor/**`) or a definition `diffPolicy.forbiddenPaths` glob. The `instructions-loaded` hook appends the loaded
file path to agentd's IPC (`POST /v1/report/instructions`); a path under `/workspace` while the binding says
`ignore`/`quote_as_context` fails the run with exit 12 (it means the suppression above regressed in a new CLI
version; the adapter contract test also catches it in CI, §7).

### 2.4 User settings (`/home/agent/.claude/settings.json`)

```json
{
  "cleanupPeriodDays": 30,
  "autoMemoryEnabled": false
}
```

`claudeCode.extraSettings` keys are merged here after the adapter removes any key in the deny set
`{permissions, hooks, disableAllHooks, allowManagedHooksOnly, mcpServers, enableAllProjectMcpServers,
enabledMcpjsonServers, disabledMcpjsonServers, allowedMcpServers, deniedMcpServers, env, apiKeyHelper,
otelHeadersHelper, model, availableModels, fallbackModel, pluginConfigs, claudeMdExcludes, autoMemoryEnabled,
autoMemoryDirectory, forceLoginMethod, sandbox, statusLine}` (removal is reported as a warning in the materialization
dry-run and fails CI publish).

### 2.5 MCP configuration (`/etc/claude-code/managed-mcp.json`)

```json
{
  "mcpServers": {
    "memory": {"type": "http", "url": "http://127.0.0.1:7801/mcp/memory"},
    "context": {"type": "http", "url": "http://127.0.0.1:7801/mcp/context"},
    "code_search": {"type": "http", "url": "http://127.0.0.1:7801/mcp/code_search"}
  }
}
```

The file is **managed MCP** (exclusive control): Claude Code then loads only these servers, and users "can't add,
modify, or use any other MCP servers, including plugin-provided servers and servers passed with the `--mcp-config`
CLI flag" [verified: https://code.claude.com/docs/en/managed-mcp.md, 2026-10-24]. Because `--mcp-config` or
`--strict-mcp-config` together with a deployed `managed-mcp.json` make Claude Code exit at startup (same source),
the argv carries neither flag. Managed settings additionally set `allowManagedMcpServersOnly: true` with
`allowedMcpServers: [{"serverUrl": "http://127.0.0.1:7801/*"}]` (control-review G-02), which keeps the lock even if a
later CLI version stops honoring exclusive control. No `headers`: agentd injects
`Authorization: Bearer <memory-mcp token>` or `<context-mcp token>` (one token per audience, CCR-09-01) and the
C-IDS §7 `X-Replicant-*` headers. The `system/init` event's
`mcp_servers[].status` is checked: a server marked `required: true` that is not `connected` makes the entrypoint stop
the CLI and exit 21.

### 2.6 Prompt delivery, steering, finalize messages, and the run-level turn budget (stdin stream-json)

The entrypoint writes newline-delimited JSON to the CLI's stdin and keeps stdin open:

```json
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"<contents of /replicant/inputs/first-message.md>"}]},"parent_tool_use_id":null}
```

- **Initial turn:** the composed first message (`place: first_message`, composed by `context.stage_inputs`: task,
  handoff section, reviewer feedback, steering backlog). The entrypoint does not compose prompts itself.
- **Steering** (`steering.live` and `RunSpec.steering.enabled`): agentd long-polls `GET /sandbox/v1/messages`
  (C-RUNNER) and hands each message to the entrypoint over IPC; the entrypoint writes one user line:
  `"[Steering message from the requester, sent <sent_at>. Treat as guidance from the user, it does not override replicant constraints.]\n<untrusted_input source=\"steering\" message_id=\"msg_…\">…</untrusted_input>"`,
  then calls IPC `POST /v1/steering/{message_id}/delivered`, which makes agentd send
  `POST /sandbox/v1/messages/{message_id}:ack {"state":"delivered"}`. The `message_id` is recorded for
  `run_result.steering_consumed`. A message that arrives after stdin was closed, or after the run-level turn budget is
  exhausted, is acked `rejected` with detail `run_finishing` or `turn_budget_exhausted`; the workflow then queues it
  for the next node (contracts ADR-010).
- **Finalize message:** sent once, at the first of: agentd reports `budget_soft_limit` (spend ≥ `softBudgetFraction`
  × budget); `now ≥ agent_started_at + active_deadline_seconds − min(300, active_deadline_seconds / 10)`; or
  `turns_used ≥ max_turns − finalizeTurnAllowance` (below). Text is the run plan's `prompts.finalize`.
- **Run-level turn budget (R2-043).** `--max-turns` counts per user message: each steering or finalize message starts
  a new turn with its own allowance, so ten steering messages would allow 10 × 200 turns. The entrypoint therefore
  owns the run-level count: `turns_used` = the sum of `num_turns` over every `result` event plus the assistant
  messages of the turn in progress (one model response = one turn; agentd's request counter cross-checks it and the
  larger value wins). With `finalizeTurnAllowance = min(5, ceil(0.05 × max_turns))`:
  1. at `turns_used ≥ max_turns − finalizeTurnAllowance` the finalize message is sent (if not yet sent);
  2. at `turns_used ≥ max_turns` no further steering is injected, stdin is closed after the current turn, and if the
     CLI is still working 30 s later it receives SIGINT. The run finalizes normally with `outcome: failed`,
     `role_result.cli.result_subtype = "error_max_turns"` and `role_result.budget.turns_used` (exit 0, §4.6).
  `--max-turns` stays on the argv with the full budget as a per-message backstop. Codex counts calls through agentd
  (`maxModelCalls`). The adapter contract test steers a scripted session ten times and asserts the run stops at the
  budget.
- **End of input:** after each `result` event the entrypoint waits `steeringGraceSeconds = 5`; if no steering
  message is pending it closes stdin, and the CLI exits after the turn (F-CC-11). If one is pending (and turn budget
  remains) it is written and a new turn starts.

### 2.7 Output event handling

Each stdout line is (1) appended verbatim to `/replicant/spool/cli_events/part-<seq:05d>.ndjson` (rotated every
30 s or 8 MiB; the WS-02 shipper redacts and uploads as `cli_events`), and (2) parsed into the normalized transcript
(`replicant.transcript/v1`, below) written to `/replicant/spool/transcript/part-<seq:05d>.ndjson` (type `transcript`).

| Event | Entrypoint action |
|---|---|
| `system` / `init` | Record `session_id`, `claude_code_version` (must equal `cliImage.version`, else exit 10 with `cli_version_mismatch`), `model`, `permissionMode` (must be `dontAsk`, else exit 12), `mcp_servers` (required check), `tools` (any of `WebFetch`/`WebSearch` present ⇒ exit 12). Emit IPC `POST /v1/status/agent-started` and write `/run/replicant/ipc/status/agent-started` (the `agent` container's `startupProbe` checks this file so the controller sets `AgentRunning=True`, XD-03-02) |
| `system` / `api_retry` | Count in transcript; no action (agentd is the retry authority) |
| `assistant`, `user` | Normalized transcript entries (`message`, `tool_use`, `tool_result`); tool inputs are truncated to 64 KiB per entry |
| `result` | Record `subtype`, `is_error`, `num_turns`, `duration_ms`, `duration_api_ms`, `total_cost_usd`, `usage`, `modelUsage`, `permission_denials`, `structured_output`, `stop_reason`, `session_id`. Then the end-of-input rule (§2.6) |
| unparseable line | Kept verbatim in `cli_events`; counted; ≥ 100 unparseable lines ⇒ exit 10 `cli_protocol_error` |

Normalized transcript record (one per line):

```json
{"schema":"replicant.transcript/v1","seq":118,"ts":"2026-10-24T08:09:12.481Z","run_id":"run_gj3h3ep3phjfs9e23v768zrca5","kind":"tool_use","turn":17,"tool":"Bash","input_preview":"go test ./internal/billing/...","tool_use_id":"toolu_01AbC","model":"anthropic/claude-sonnet-5-5"}
```

`kind ∈ {session_start, user_message, assistant_text, thinking_summary, tool_use, tool_result, steering, finalize_prompt, api_retry, result, error}`.
Thinking blocks are not copied (only a length). WS-08 owns redaction and which records are exported to Langfuse.

### 2.8 Session capture and the session-resume rule SR-1 (D-14; owner of the single rule, R2-053)

This section is **the** session-resume rule. WS-04 (ADR-04-001, working-memory.md §4) and WS-06 (upgrades.md) cite it
instead of restating it (XD-FIX-R2-053).

**SR-1. A CLI session is resumed only when all of the following hold; otherwise the run starts a fresh session and
receives the handoff (WS-04 handoff, §4.8):**

| # | Condition | Checked by |
|---|---|---|
| 1 | Role is `implementer` or `repair`, and the prior capture comes from the **same job and the same cycle** (same-cycle re-invocation, for example after QA failed). Planner, reviewer, QA, and takeover never resume | workflow (`continuity_for()`, WS-04) when staging; prepare re-checks `cycle` |
| 2 | Header `cli_image_digest` equals the digest in this run's `plan.agent.cliImage.image` (identical CLI image, not "same major.minor") | prepare |
| 3 | Header `created_at` is less than 24 h before `prepare` starts, and `size_bytes` ≤ 256 MiB | prepare |
| 4 | Header `head_tree_sha` equals `git write-tree` of the workspace after applying the staged diff to `base_sha` | prepare |
| 5 | Header `workspace_path` is `/workspace/repo` and `cli` equals `plan.agent.cli` | prepare |
| 6 | The job has not been migrated to another bundle since the capture (bundle migrations, CLI upgrades, and model retirement always use the handoff, never cross-version resume) | workflow |

Condition failures in 2–5 make prepare exit **42** (`SessionRestoreFailed`, non-retryable); the workflow then re-runs
the node fresh with the handoff (C-CRD §4). Workflows stage a session only when 1 and 6 hold, so 42 indicates a bug or
an expired capture.

- **Capture** (`session.capture`; roles that never resume run with `--no-session-persistence` / Codex `--ephemeral`):
  after the CLI exits, the entrypoint writes a tar whose **first member** is `replicant-session.json`
  (`replicant.run-plan/v1#/$defs/session_header`: `{cli, cli_version, cli_image_digest, session_id, workspace_path,
  base_sha, head_tree_sha, created_at, size_bytes, run_id, cycle}`), followed by
  `/home/agent/.claude/projects/replicant-workspace/<session_id>.jsonl` and the session's side directory (or the Codex
  rollout file), zstd-compressed to `/replicant/out/cli_session_state/<cli>-session.tar.zst` (type `cli_session_state`,
  bucket `session`, raw-restricted, `job37d`). A capture above 256 MiB is not uploaded (it could never be resumed).
  The handoff's `mechanical.session` points at it. **Memory text is never captured** (XD-W2-04-01): before taring,
  the entrypoint replaces every occurrence of the memory block bytes (`memory-injection.json` `.rendered`) in the
  session files with `[replicant:memory-block sha256=<hex>]`. Claude Code's session JSONL does not record the appended
  system prompt, so this normally finds nothing; Codex rollouts may record the global `AGENTS.md` as instructions
  (U-14, §9), which this step strips. A resumed session rebuilds its system prompt from the current joined prompt
  (`--system-prompt-snapshot off`; Codex re-reads `AGENTS.md`), so the placeholder is never shown to a model as
  instructions.
- **Restore:** `context.stage_inputs` server-side copies the capture into this run's `session` bucket `inputs/` and
  lists it with `place: restore_session` and the header copy in `session`. prepare verifies the object digest, reads
  the tar's `replicant-session.json`, requires it to equal the manifest copy, applies SR-1 checks 2–5, and unpacks into
  `/home/agent/.claude/projects/replicant-workspace/` (`CLAUDE_CODE_PROJECT_DIR_NAME`, so the directory name never
  depends on the cwd) or `$CODEX_HOME/sessions/…`.
- **Resume flags:** Claude `--resume <session_id> --fork-session --system-prompt-snapshot off`; Codex
  `codex exec … resume <thread_id> -` (§3.1).

### 2.9 Decision: CLI vs Agent SDK

Decided in [ADR-002](adr/002-claude-code-cli-over-agent-sdk.md): **the CLI for every Claude role in v1.** Summary:
the Agent SDK drives the same CLI binary over the same stream-json protocol (the Python SDK 0.2.174 bundles CLI
2.1.310 at `claude_agent_sdk/_bundled/claude` [verified: https://pypi.org/project/claude-agent-sdk/0.2.174/,
2026-10-24]). What it adds over the CLI is in-process Python/TypeScript callbacks (`can_use_tool`, function hooks,
in-process MCP tools), which Replicant does not need: permissions are static per run (managed settings), hooks are
commands into the entrypoint binary, and MCP tools are remote Replicant services. Using the SDK would require a
Python or Node runtime in every toolchain image (the toolchains are diverse) and would make the SDK's own defaults a
second configuration surface (for example, the SDK loads user, project, and local settings when
`setting_sources` is unset [verified: https://code.claude.com/docs/en/agent-sdk/python.md, 2026-10-24]). The
reserved `cli: claude-agent-sdk` value lets a later bundle introduce an SDK runner (for example for an evaluation
harness that needs per-call tool vetoes) without a contract change.

---

## 3. Codex

### 3.1 Invocation (exact argv)

Reviewer example (`examples/agent-reviewer-codex.yaml`):

```bash
exec /opt/replicant/cli/codex/0.164.0/codex exec \
  --json \
  --model openai/gpt-5-codex-2025-09-15 \
  --sandbox danger-full-access \
  --skip-git-repo-check \
  --ignore-rules \
  --cd /workspace/repo \
  --output-schema /home/agent/.replicant/agent-report.schema.json \
  --output-last-message /replicant/out/.cli/last-message.json \
  -c 'model_provider="replicant"' \
  -c 'projects."/workspace/repo".trust_level="untrusted"' \
  -c 'project_doc_max_bytes=0' \
  -c 'project_root_markers=[]' \
  -c 'web_search="disabled"' \
  -c 'history.persistence="none"' \
  -c 'shell_environment_policy.inherit="core"' \
  -c 'shell_environment_policy.ignore_default_excludes=false' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_reasoning_summary="concise"' \
  -c 'model_verbosity="medium"' \
  -
```

Resume (repair on Codex, `place: restore_session`): `codex exec --json <same options> resume <thread_id> -`
`[unverified: option placement before vs after the resume subcommand; see section 9 U-5. The adapter contract test
pins the working form per CLI version]`.

Rendering rules (adapter package `adapter/codex`):

| Field | Rendered as |
|---|---|
| `codex.sandboxMode` | `--sandbox`. `danger-full-access` is the default: the AgentRun (gVisor or Kata) is the sandbox, and Codex's bwrap sandbox needs unprivileged user namespaces, which `drop: [ALL]` and gVisor/Kata configurations do not guarantee (F-CX-7). `read-only` is allowed for reviewers that run no tests. `workspace-write` is allowed only after WS-02 verifies bwrap under the chosen runtime (§9 U-6) |
| approval | not passed: exec hard-codes `never` (F-CX-2). `codex.approvalPolicy` must be `never` (schema) |
| `tools.shell.deny` | Codex has no per-command deny list in exec. The run plan's role prompt states them (composed into `AGENTS.md`), and the diff collector and integrity check enforce after the fact; the lint forbids `shell.mode: allowlist` for Codex |
| `tools.edit: false` | Instruction only (Codex cannot remove its patch tool); enforced by `workspaceIntegrity` (§6) |
| `mcpServers` | `[mcp_servers.<name>]` with `url = "http://127.0.0.1:7801/mcp/<service>"`, `required`, `tool_timeout_sec`, `enabled_tools`/`disabled_tools` |
| `limits.maxTurns` | Not expressible (one exec turn contains many model calls). The entrypoint enforces `limits.maxModelCalls` through agentd's per-run request counter |
| `limits.maxCostUsd` | Gateway only (Codex has no client budget flag) |
| composed system append (role, constraints, memory) | `$CODEX_HOME/AGENTS.md` → the joined prompt `/run/replicant/prompt/system-append.md` (§1.4, XD-W2-04-01). `prompts.role.mode: replace` uses `model_instructions_file = "/run/replicant/prompt/system-append.md"` instead |
| composed first message | stdin (`-`), then stdin is closed (no mid-run input in exec) |
| `outputContract.structuredResult = cli_schema` | `--output-schema` + `--output-last-message` |

### 3.2 `$CODEX_HOME/config.toml`

```toml
# Written by replicant-entrypoint run. Security-relevant keys are repeated as -c overrides (highest precedence).
model = "openai/gpt-5-codex-2025-09-15"
model_provider = "replicant"
web_search = "disabled"
project_doc_max_bytes = 0
project_root_markers = []

[model_providers.replicant]
name = "Replicant AI gateway (via agentd loopback)"
base_url = "http://127.0.0.1:7800/openai/v1"
wire_api = "responses"
request_max_retries = 2          # agentd retries 429/5xx first (gateway.md section 6)
stream_max_retries = 3
stream_idle_timeout_ms = 300000
env_http_headers = { "traceparent" = "REPLICANT_CLI_TRACEPARENT" }   # XD-08-03: cli.invocation span context, set by the entrypoint
# no env_key, no requires_openai_auth: Codex sends no Authorization header, agentd injects the ai-gateway token

[projects."/workspace/repo"]
trust_level = "untrusted"

[history]
persistence = "none"

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false

[mcp_servers.context]
url = "http://127.0.0.1:7801/mcp/context"
required = false
tool_timeout_sec = 60

[mcp_servers.code_search]
url = "http://127.0.0.1:7801/mcp/code_search"
required = false
tool_timeout_sec = 60

[otel]
environment = "replicant-prod"
log_user_prompt = false
exporter = "none"                # XD-08-03: Codex log events carry tool output snippets outside the redactor (D-21)
trace_exporter = { otlp-http = { endpoint = "https://otel-sandbox.prod.replicant.internal:4318/v1/traces", protocol = "binary" } }
metrics_exporter = { otlp-http = { endpoint = "https://otel-sandbox.prod.replicant.internal:4318/v1/metrics", protocol = "binary" } }
```

The endpoints are rendered from the standard `OTEL_EXPORTER_OTLP_ENDPOINT` (controller-provided, C-IDS §7) at run
time, never hard-coded (XD-FIX-R1-041); the shown value is production's. The CA comes from the standard
`OTEL_EXPORTER_OTLP_CERTIFICATE` variable, which the Codex child inherits. Trace context reaches the gateway two ways: `env_http_headers` sends the entrypoint's
`cli.invocation` span as `traceparent` (documented key, F-CX-5), and with `trace_exporter` configured Codex also
injects the current span's `traceparent` (source-verified only, F-CX-9). agentd keeps whichever arrives and creates
one if neither does, so propagation never depends on the source-only behavior (settles R1-076 with WS-08's OQ-08-05;
XD-FIX-R1-076).

### 3.3 Event handling

| Event | Action |
|---|---|
| `thread.started` | Record `thread_id` (session id for capture/resume); report agent-started (as §2.7) |
| `item.completed` `command_execution` | Transcript `tool_use`/`tool_result` (command, exit_code, output truncated 64 KiB) |
| `item.completed` `file_change` | Transcript; if `tools.edit = false`, mark `integrity_suspect` (the post-run integrity check decides) |
| `item.completed` `mcp_tool_call` | Transcript |
| `item.completed` `web_search` | Exit 12 (`web_search` should be disabled; config regression) |
| `item.completed` `agent_message` | Transcript; last one is the structured result candidate |
| `turn.completed` | Record `usage`; done |
| `turn.failed` / `error` | Record; classification per §4.6 using agentd state |

### 3.4 Codex sandbox choice inside an AgentRun

Codex's documentation says to use `danger-full-access` when the container is the intended boundary (F-CX-7). The
Replicant follows it: isolation is the AgentRun runtime (D-06), default-deny egress (D-21), no credentials in the agent
container (ADR-001), and the output contract. The consequence is that `edit: false` cannot be enforced *inside* Codex,
which is why read-only intent is enforced by integrity checking and output contracts instead (§6).

### 3.5 Live steering on Codex

Not available with `exec` (F-CX-2). App-server `turn/steer` works but is documented as experimental and not
supported for production (F-CX-12). Decision ([ADR-003](adr/003-codex-exec-over-app-server.md)): Codex agents
declare `steering.live: false` (schema-enforced for `mode: exec`), so the Runner returns `steering_not_supported`
and the workflow queues steering for the next node. Reviewer runs are short (median target under 15 minutes), so
this costs little. Revisit when app-server is supported for production.

---

## 4. Runner entrypoint (`replicant-entrypoint`)

### 4.1 Binary and subcommands

Static Go 1.25 binary (`CGO_ENABLED=0`, `-trimpath`, reproducible build), at `/opt/replicant/bin/replicant-entrypoint`
in the CLI image. Subcommands:

| Subcommand | Runs in | Purpose |
|---|---|---|
| `prepare` | init container `prepare` | Fetch and verify the manifest and every input object (including `run-plan.json`, `system-append.md`, `first-message.md`); check out the repo (blobless, through agentd's git route); apply diff; restore session under SR-1; render the managed layer; copy `/opt/replicant` into the `tools` volume |
| `run` | `agent` container, PID 1 | Re-materialize `$HOME`; wait for services; bootstrap; launch the CLI (or, for role `qa`, the `replicant-qa` harness, §4.10); supervise; enforce the run-level turn budget; finalize; write outputs; exit with the contract code |
| `hook <name>` | invoked by Claude Code hooks | `instructions-loaded`, `guard-paths`, `session-end` |
| `materialize --manifest <manifest.json> --plan <run-plan.json> --out <dir> --print-argv` | CI only (config repo and CLI image CI) | The dry-run: produce the exact `$HOME`, managed files, env, and argv without running anything (config-publishing.md §2 step 7) |
| `contract-test` | CLI image CI; WS-11 contract suite with the `contract-probe` agent (XD-11-09) | Adapter contract tests against the real CLI binary and the gateway shim in `script`/`replay` mode (§7) |
| `version` | anywhere | Prints entrypoint version, CLI name and version, adapter version, replicant-qa version when present |

### 4.2 Bootstrap configuration (`/etc/replicant/bootstrap.json`)

Non-secret, environment-level values the controller writes into ConfigMap `ar-<body>-config` (C-CRD §6) and
mounts at `/etc/replicant/bootstrap.json` in `prepare` and `agentd` (schema `replicant.sandbox-bootstrap/v1`, XD-03-03):

```json
{
  "schema": "replicant.sandbox-bootstrap/v1",
  "env": "prod",
  "run_id": "run_gj3h3ep3phjfs9e23v768zrca5",
  "inputs_manifest_uri": "s3://acme-replicant-prod-artifacts/tenants/payments/jobs/job_01m5p7x7r0bt69j0h0fbxepw90/runs/run_gj3h3ep3phjfs9e23v768zrca5/inputs/manifest.json",
  "inputs_manifest_sha256": "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b",
  "endpoints": {
    "runner_sandbox": "https://runner-sandbox.prod.replicant.internal",
    "ai_gateway": "https://ai-gateway.prod.replicant.internal",
    "memory_mcp": "https://memory-mcp.prod.replicant.internal/mcp",
    "context_mcp": "https://context-mcp.prod.replicant.internal/mcp",
    "code_search_mcp": "https://context-mcp.prod.replicant.internal/mcp/code-search",
    "repo_mirror": "https://repo-mirror.prod.replicant.internal",
    "otlp": "https://otel-sandbox.prod.replicant.internal:4318",
    "log_transport": "stdout"
  },
  "package_mirror_host": "artifactory.acme.internal",
  "package_mirrors": {
    "npm": "https://artifactory.acme.internal/api/npm/npm-virtual/",
    "pypi": "https://artifactory.acme.internal/api/pypi/pypi-virtual/simple",
    "goproxy": "https://artifactory.acme.internal/api/go/go-virtual"
  },
  "repo": {"repo_id": "R_kgDOK2a9Xw", "base_sha": "8c1e4f2a9b7d3c6e5f0a1b2c3d4e5f6a7b8c9d0e", "fetch_depth": 50},
  "kms_key_arn": "arn:aws:kms:us-east-1:123456789012:alias/replicant/prod/tenant/payments"
}
```

Fields WS-03 consumes beyond the core set (XD-W2-02-07, XD-FIX-R1-041, XD-FIX-R2-039):

- `endpoints.otlp` is the sandbox OTLP gateway `https://otel-sandbox.<env>.replicant.internal:4318` (CA
  `/etc/replicant-ca/ca.crt`, ConfigMap `replicant-ca-bundle`); the controller also sets the standard
  `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, and `OTEL_EXPORTER_OTLP_CERTIFICATE`
  in the agent and agentd containers. The entrypoint, agentd, and the Codex config read **only** these standard
  variables (no hard-coded host); the entrypoint passes them through to the CLI child (§2.2, §3.2).
- `endpoints.log_transport` (`stdout`, default, or `otlp`): in `otlp` mode the entrypoint and agentd send each JSON
  log line as an OTLP log record (body = the line, attribute `replicant.container` = `agent` or `agentd`) to
  `$OTEL_EXPORTER_OTLP_ENDPOINT/v1/logs` instead of writing it to stdout; termination messages still go to
  `/dev/termination-log` (XD-FIX-R2-039, B-LOG-01).
- `package_mirror_host` (`artifactory.acme.internal`, or `pkg.<env>.replicant.internal` where WS-02's fast path is
  enabled) is the host every package-manager file of §1.4 points at; `package_mirrors.*` give the full virtual
  repository URLs on that host. For Go the entrypoint materializes (fast-path.md §5): `GOPROXY=https://<package_mirror_host>/api/go/go-virtual`,
  `GOPRIVATE` and `GONOSUMDB` = the environment definition's private module patterns, `GOFLAGS=-mod=mod`, and in
  `/home/agent/.gitconfig` `url."http://127.0.0.1:7802/git/".insteadOf` entries for the private module hosts, so
  private modules come through agentd's read-only git relay (granted repos only).

### 4.3 Lifecycle

```mermaid
sequenceDiagram
  autonumber
  participant CT as agentrun-controller
  participant AG as replicant-agentd (sidecar)
  participant PR as prepare (init)
  participant EP as replicant-entrypoint run
  participant CLI as claude / codex
  participant SH as replicant-gateway-shim
  participant UP as uploader (WS-02)
  CT->>AG: start native sidecar (token mounted)
  AG->>AG: load token, GET /v1/budget from the shim, readyz OK
  CT->>PR: start init container
  PR->>AG: GET :7803/inputs/manifest.json (written by context.stage_inputs)
  PR->>PR: verify sha256 = inputs_manifest_sha256 (else exit 40)
  PR->>AG: GET :7803/inputs/<each object>, verify sha256 (else exit 40)
  PR->>AG: git fetch :7802/git/<repo_id> base_sha (else exit 43)
  PR->>PR: checkout base_sha, apply diff (else exit 41), restore session (else exit 42)
  PR->>PR: write /etc/claude-code/*, place inputs under /replicant/inputs, copy /opt/replicant
  PR->>AG: IPC POST /v1/status/prepared (closes :7802 and :7803)
  PR-->>CT: exit 0 (WorkspaceReady)
  CT->>EP: start agent container
  EP->>EP: wait services ready, run seeds and bootstrap steps (exit 44/45 on failure)
  EP->>EP: re-materialize $HOME, integrity snapshot (read-only roles)
  EP->>CLI: spawn (argv, env), stdin task message
  CLI-->>EP: system/init → status file agent-started (startupProbe → AgentRunning)
  CLI->>AG: POST :7800/anthropic/v1/messages (placeholder auth)
  AG->>SH: + Bearer run token, X-Replicant-*, traceparent
  SH-->>AG: 200 + budget headers
  AG-->>CLI: response
  AG-->>EP: IPC events (budget_soft_limit, steering, gateway_unavailable)
  CLI-->>EP: result event, stdin closed, CLI exits
  EP->>EP: kill(-1, SIGKILL), collect outputs (diff, handoff, session, integrity)
  EP->>AG: GET IPC /v1/usage, POST /v1/status/finalizing
  EP->>EP: compose + validate run_result, write outputs.json, termination message
  EP-->>CT: exit code (section 4.6)
  AG->>SH: POST /v1/runs/self:close
  UP->>UP: upload entries, write manifest.json (C-ART)
```

### 4.4 Config materialization algorithm (normative)

1. **prepare** reads `/etc/replicant/bootstrap.json` and the manifest through agentd; verifies
   `sha256(manifest) == inputs_manifest_sha256`; validates it against `replicant.run-inputs/v1` (embedded schema; exit 40
   `unsupported_manifest_schema` otherwise). The manifest has exactly one writer, `context.stage_inputs`
   (XD-FIX-R1-067).
2. Downloads every listed object, verifies sha256 and size, and places it per `place`: `run_plan` →
   `/replicant/inputs/run-plan.json` (validated against `replicant.run-plan/v1`), `system_append` →
   `/replicant/inputs/system-append.md`, `system_append_memory` → `/replicant/inputs/memory-injection.json` (validated
   against `replicant.memory-injection/v1`; at most one), `first_message`, `context_file` →
   `/replicant/inputs/context/<path>`, `qa_file` → `/replicant/inputs/qa/<path>` (including retry checkpoints at
   `qa/checkpoints/<stage>.json`, XD-W2-05-04), `apply_diff`, `restore_session` (SR-1, §2.8).
3. Verifies `plan.agent` is the agent definition it was built for: the CLI image's `cli` and version must equal
   `plan.agent.cli` and `plan.agent.cliImage.version` (else exit 40 `cli_mismatch`); for role `qa`, the image must also
   carry `replicant-qa` at `plan.agent.qaHarness.version`.
4. Verifies every `plan.prompts.*.sha256` (the finalize prompt is used directly; the others are provenance for the
   composed files).
5. Renders the **security layer** (managed settings and managed MCP for Claude Code; `-c` override list for Codex) into
   the `managed` volume. It depends only on the plan, never on workspace content.
6. **run** (after services and bootstrap) renders the **user layer** (`$HOME` files in §1.4) from the plan, deleting
   any pre-existing CLI config files in `$HOME` first (`~/.claude/settings.json`, `~/.claude/CLAUDE.md`,
   `~/.claude/{agents,commands,skills,plugins,rules}/`, `~/.codex/config.toml`, `~/.codex/*.config.toml`,
   `~/.codex/AGENTS*.md`, `~/.codex/{prompts,rules}/`, `~/.agents/`), so bootstrap scripts cannot plant CLI
   configuration. It then writes the joined prompt `/run/replicant/prompt/system-append.md` (§1.4: re-verifies the
   sha256 of `system-append.md` and `memory-injection.json` against the manifest, joins, `chmod 0444`) and, for
   Codex, links `~/.codex/AGENTS.md` to it. The joined prompt is the only file that holds memory text next to the
   role prompt; it lives on tmpfs and is never uploaded (XD-W2-04-01).
7. Starts span `cli.invocation` (child of `sandbox.run`, which spans the whole entrypoint run, XD-08-03), writes
   `/replicant/spool/control/cli-invocation.json` (`{cli, version, image_digest, argv (prompt values replaced by their
   sha256), env names, session mode, started_at, traceparent}`), renders argv and env (§2.1–2.2 / §3.1), and starts the
   CLI as a child (not `exec`: PID 1 remains the entrypoint).

### 4.5 Signal handling and finalization

| Trigger | Entrypoint behavior |
|---|---|
| CLI exits by itself | Finalize (below) |
| agentd IPC `cancel_requested` `{reason}` | agentd raises it when a heartbeat response carries `cancel_reason` (mirroring `inbox/_control.json` = `{"cancel_reason","at"}`, written with `If-None-Match: *` by runner-api or the controller; XD-FIX-R2-040). The entrypoint records the reason and starts the SIGTERM path below without waiting for the signal |
| agentd IPC `heartbeat_lost` | No successful heartbeat for **120 s** (the self-stop timeout C-RUNNER 3.0.0 delegates to WS-02; XD-W2-02-07): SIGINT the CLI, finalize as for a cancel with reason `lease_lost` (partial outputs, session capture kept), exit 143; the uploader finalizes normally. A partitioned sandbox therefore stops spending even if the gateway still answers |
| SIGTERM (controller: cancel, deadline, takeover, security) | Determine the cancel reason: first the IPC `cancel_requested` reason (heartbeat / `inbox/_control.json`, XD-FIX-R2-040); if none has arrived, `POST /v1/heartbeat:now` to agentd, which sends an immediate heartbeat and returns its `cancel_reason` (bounded at 3 s, R1V-022); else the downward-API annotation `replicant.example.com/cancel-reason` at `/etc/podinfo/annotations` (CCR-02-03, the second channel); if the reason is still unknown, finalize **as for `security`** (no workspace archive, no session capture), the fail-safe choice, since a takeover can still be reconstructed from the diff bundle and handoff; send SIGINT to the CLI process group; after `limits.cliGraceSeconds` send SIGTERM; after 5 more seconds SIGKILL; then finalize with `partial=true`. Reason `takeover`: also archive the workspace (§4.8). Reason `security`: **no** workspace archive and no session capture (the workspace is evidence of a policy violation and stays inside the pod until deletion); only transcripts already spooled and `run_result` are written. Total budget: `timeouts.finalize_seconds` − 30 s reserved for the uploader |
| SIGINT / SIGHUP / SIGQUIT | Ignored by PID 1 (only the controller ends the run) |
| agentd IPC `budget_exceeded` | Give the CLI up to 30 s to end its turn (it receives a non-retryable error, gateway.md §6), then SIGINT; finalize; exit 20 |
| agentd IPC `gateway_unavailable` (continuous failure > `gateway.unavailableAfterSeconds`) | SIGINT; finalize; exit 21 |
| Watchdog signal (WS-02 writes `/run/replicant/ipc/status/disk-exceeded`) or `ENOSPC` observed in CLI events | SIGINT; finalize with whatever fits; exit 30 |
| CLI killed by the kernel OOM killer (cgroup v2 `memory.events` `oom_kill` increased) | Finalize; exit 31 |
| Entrypoint internal panic | Recovered at top level; writes termination message; exit 10 with `entrypoint_panic` |

Finalize steps (each bounded by a timer; a step that times out is skipped and recorded):

1. `kill(-1, SIGKILL)` from PID 1: removes every remaining agent-uid process (background jobs, daemons). From here on the
   workspace cannot change.
2. Collect outputs per `outputContract` (§4.8).
3. Session capture per `session.capture`.
4. Query agentd: `GET /v1/usage` (token counts, model calls, spend, rate-limit wait) and `/v1/steering/consumed`.
5. Compose `run_result` (§4.7), validate against the contract envelope and the role schema (both embedded in the
   binary). Valid ⇒ write `/replicant/out/run_result/result.json`.
6. Write `/replicant/out/outputs.json` (`replicant.run-outputs/v1`) via write-to-temp + `rename(2)`.
7. Write `/dev/termination-log` (≤ 1,024 bytes, F-K8S-1 divides 12 KiB across up to 12 containers). For exit 20 the
   message **starts with the budget scope** `run:`, `job:`, or `tenant_period:` (WS-01 parses it, XD-01-04, R1-071):
   `{"code":20,"reason":"GatewayBudgetExceeded","message":"run: budget 15.00 USD exhausted at 15.04 USD (job remaining 16.50)"}`.
   The controller copies `message` into the `Succeeded` condition, from which `RunStatus.failure.message` is read.
8. `POST /v1/status/finished` to agentd (agentd then closes the run at the shim) and exit.

### 4.6 Exit-code contract (normative; WS-02 maps codes to C-CRD reasons from container status, XD-03-02)

| Exit | Container | C-CRD reason | Retryable | Meaning | `run_result` written? |
|---|---|---|---|---|---|
| 0 | agent | — (phase Succeeded) | — | CLI (or QA harness, exit classes `pass`, `pass_with_warnings`, `fail`, `inconclusive_other`, `not_applicable`) finished; valid `run_result` (any `outcome`, including `failed` after the run-level turn budget, §2.6; for qa the outcome follows the verdict mapping in §4.10) | yes |
| 10 | agent | `AgentExitedNonZero` | yes | CLI or harness crashed, protocol error, version mismatch, entrypoint panic | no |
| 11 | agent | `AgentResultMissing` | yes | CLI ended but produced no structured result (`structured_output` absent; Codex last message missing; QA harness wrote no report) and no limit explains it | no |
| 12 | agent | `AgentResultInvalid` | no | Structured result fails the role schema; `workspaceIntegrity: enforce` violated; forbidden instruction file loaded; forbidden path with `onForbiddenPath: fail`; config regression detected (web tools present, wrong permission mode) | no |
| 20 | agent | `GatewayBudgetExceeded` | no | Gateway returned 402 (`budget_exceeded`, `tenant_quota_unset`, `account_unset`), or Claude `result.subtype = error_max_budget_usd`, or `maxModelCalls`/token caps hit. Termination message prefixed `run:` / `job:` (also budget account) / `tenant_period:` (also platform ceiling and unset quota) (§4.5 step 7, gateway.md §6.6) | yes (`outcome: failed`, partial outputs incl. handoff) |
| 21 | agent | `GatewayUnavailable` | yes | Gateway or a required MCP server unavailable beyond the configured window; persistent 401 from the gateway after one token refresh; 503 `run_policy_missing` that does not clear within 120 s | yes (partial) |
| 30 | agent | `WorkspaceDiskExceeded` | no | Watchdog or ENOSPC | yes if it fits (partial) |
| 31 | agent | `OOMKilled` | no | CLI, harness, or a child killed by the OOM killer inside the container | yes (partial) |
| 40 | prepare | `InputsFetchFailed` | no | Manifest/object missing, digest mismatch, unsupported schema, CLI mismatch | no |
| 41 | prepare | `DiffApplyFailed` | no | `git apply --binary --index` failed against `base_sha` | no |
| 42 | prepare | `SessionRestoreFailed` | no | SR-1 checks 2–5 failed (§2.8) or unpack failed | no |
| 43 | prepare | `RepoFetchFailed` | yes | repo-mirror fetch failed or `base_sha` absent | no |
| 44 | agent | `EnvironmentSetupFailed` (CCR-03-01, accepted; C-CRD 2.3.0) | yes | A bootstrap step with `onFailure: fail` failed; for role `qa`: harness exit class `environment_setup` | yes (`outcome: failed`; non-qa: `role_result.bootstrap.ok=false`; qa: `verdict: inconclusive`, `agentrun_exit_class: environment_setup`, **no** bootstrap object, the harness runs bootstrap itself; XD-FIX-R1-116) |
| 45 | agent | `EnvironmentSetupFailed` | yes | A service did not become ready or its seed failed; for role `qa`: harness exit class `environment_services` | yes (`outcome: failed`; qa: `verdict: inconclusive`, `agentrun_exit_class: environment_services`, no bootstrap object) |
| 143 | agent | `CancelledByRequest` / `DeadlineExceeded` (controller knows which) | — | SIGTERM-driven finalize, or the 120 s heartbeat self-stop (the controller reports `NodeLost` for a lapsed lease) | yes if composed in time (partial) |
| 143 | agent | `SecurityViolation` (cancel reason `security`, CCR-09-04; `Cancelling → Failed`) | no | Security automation cancelled the run; finalize skips the workspace archive and session capture (§4.5) | yes if composed in time |

The controller also distinguishes pod-level OOM (whole sandbox killed: `OOMKilled` without finalization, C-CRD §1) and
pod kills by security automation (`Running → Failed / SecurityViolation`, no finalization).

### 4.7 Result composition

`run_result` (C-RUNNER `run-result.schema.json`) fields and their **sources**:

| Field | Source (trust) |
|---|---|
| `run_id`, `job_id`, `role` | manifest (trusted) |
| `outcome` | `agent_report.outcome`, overridden by the entrypoint: diff empty and contract allows ⇒ `no_changes`; limit hit (max turns, budget, deadline) ⇒ `failed`; integrity violation with `detect` ⇒ `failed` |
| `summary` | `agent_report.summary` (untrusted model text, ≤ 8000 chars; C-RUNNER marks it untrusted) |
| `outputs` | keys of the artifacts actually produced (bucket-relative) |
| `usage.wall_seconds` | entrypoint clock (CLI start → exit) |
| `usage.model_calls`, `input_tokens`, `output_tokens`, `cache_*` | **agentd** counters (trusted), parsed from gateway responses |
| `usage.cost_usd_reported` | agentd: last `X-Replicant-Budget-Run-Spent-Usd` (gateway-computed). The cost ledger stays authoritative |
| `usage.peak_memory_bytes` | cgroup v2 `memory.peak` of the agent container cgroup, read at finalize `[unverified: memory.peak visibility inside gVisor/Kata guests, section 9 U-7]`; fallback: watchdog-sampled max of `memory.current` |
| `usage.peak_workspace_bytes` | watchdog high-water mark (WS-02, via `/run/replicant/ipc/status/disk-peak`) |
| `memory_entries_used` | manifest `memory_entries` |
| `steering_consumed` | agentd delivered set |
| `role_result` | role schema: `agent_report` from the CLI structured output; `cli` from events; `budget` from agentd; `changes`/`integrity`/`archive` computed by the entrypoint after `kill(-1)` |

When a limit stops the CLI before it produced a structured result (max turns, budget, deadline, cancel), the
entrypoint synthesizes `agent_report = {"outcome":"failed","summary":"[replicant] <reason>; see handoff","tests_run":[]}`
so the result still validates; a synthesized report always starts with `[replicant]`.

### 4.8 Output collection (after `kill(-1)`)

All git commands run as uid 10001 with a hardened environment, because the repository's `.git/config` and
`.gitattributes` are agent-controlled: `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/opt/replicant/etc/gitconfig`
(read-only), `GIT_ATTR_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, and `-c core.hooksPath=/dev/null -c core.fsmonitor=false
-c diff.external= -c core.pager=cat --no-ext-diff --no-textconv`; filters declared in the repo config are
neutralized by running `git add` with `-c filter.<name>.clean= -c filter.<name>.smudge= -c filter.<name>.process=`
for every `filter.*` section present (read with `git config --file .git/config --get-regexp '^filter\.'`). Output
is parsed, never executed.

| Output | Procedure |
|---|---|
| `diff_bundle` | `GIT_INDEX_FILE=/replicant/out/.cli/index git read-tree <base_sha>`; `git add -A -- . ':(exclude)<forbidden>'…` into that temporary index; `git diff --cached --binary --full-index <base_sha>` → `changes.patch`; reject symlinks pointing outside the repo, submodule (gitlink) changes unless allowed, mode changes unless allowed, binary files unless allowed, more than `maxFilesChanged` files or `maxPatchBytes`; D-10 agent-config paths and `forbiddenPaths` are excluded and listed in `forbidden_paths_stripped`. If the agent made commits, `git format-patch <base_sha>..HEAD --stdout` → `commits.mbox` (informational; the Publisher builds commits from `changes.patch`). `metadata.json` = `{base_sha, files_changed, insertions, deletions, paths[], forbidden_paths_stripped[], created_by_run}`. Tar + zstd → `diff_bundle/changes.tar.zst` (C-ART content type `application/vnd.replicant.diff-bundle.v1+tar+zstd`) |
| `handoff` | `handoff/handoff.json`, validated against WS-04's `replicant.handoff/v1` (https://schemas.replicant.example.com/ws04/handoff/v1.json, R1-068). `producer` from the manifest and plan (`node_key`, `cycle`, `ordinal` from `logical_activity_id`; `run_outcome` from the final outcome); `goal` copied verbatim from the task packet's `goal` object (`/replicant/inputs/context/task_packet.json`, XD-FIX-R1-068); **`mechanical`** computed by the entrypoint after `kill(-1)`, never from model text: `base_sha`, `head_tree_sha` (`git write-tree` of the collection index), `diff_bundle_key`, `diff_stats`, `files_touched[]` (`git diff --name-status -M` + numstat, ≤ 500), `commands[]` (deduplicated `Bash` tool_use / Codex `command_execution` items with exit codes, counts, `classified_as` by prefix rules, last 200), `tests[]` (from `agent_report.tests_run`, upgraded with JUnit files the agent left under `/workspace/.replicant-test-results/` when present), `memory_injected` (manifest), `steering_consumed` (agentd), `context{compactions, turns}` (CLI events), `session` (pointer to this run's capture, §2.8). **Narrative:** the agent's `/replicant/out/handoff.md` (role and finalize prompts ask for it) is carried as `narrative_markdown` with `narrative_status = markdown_only` (≤ 64 KiB, truncated from the end → `truncated`); if absent, the entrypoint synthesizes it from `agent_report.summary` (`synthesized: true`), and if that is absent too, `narrative_status = missing` |
| `plan` | Planner writes `/replicant/out/plan.md` and returns steps, `acceptance_criteria[]` (IDs `ac<n>`), and `expected_ui_changes[]` in `agent_report` (R1-075, XD-05-05); entrypoint writes `plan/plan.json` (the full `agent_report`) and `plan/plan.md` |
| `review_report` | `review_report/review.json` = `agent_report` + `integrity` + `cli` |
| `memory_candidates` | Agent may write `/replicant/out/memory_candidates.json` (WS-04 schema); the entrypoint validates and passes it through (untrusted; WS-04 curates) |
| `workspace_archive` | Role `takeover` or cancel reason `takeover`: `tar --exclude-vcs-ignores` of `/workspace/repo` including untracked files and `.git` (so the user gets history), zstd, ≤ 10 GiB (C-ART) |
| `cli_session_state` | §2.8 / §3 (SR-1 header first; not captured for cancel reason `security`) |
| QA types (`qa_report`, `junit_xml`, …, `qa_generated_flow`) | Role `qa` only: written by `replicant-qa` under `/replicant/out/qa/<type>/<name>` (checkpoints under `/replicant/out/qa-checkpoints/<stage>.json`, published as `qa_report/checkpoints/<stage>.json`) with its own entries list; merged into `outputs.json` (§4.10) |

### 4.9 Failure modes

| Failure | Detection | Result | User-visible |
|---|---|---|---|
| Inputs digest mismatch | prepare | exit 40, non-retryable (workflow bug) | "Run inputs corrupted" + operator alert `ReplicantRunInputsInvalid` |
| repo-mirror down | prepare | exit 43, retryable | timeline shows retry |
| `npm ci` fails | bootstrap step | exit 44 | "Environment setup failed: install-npm (exit 1)"; tenant-visible log link |
| CLI binary missing for libc | prepare detects (`ldd`-free probe: execute `claude --version`) | exit 40 `cli_incompatible_image` | operator alert (environment image incompatible) |
| CLI hangs without output | no stdout for `hangTimeoutSeconds = 900` and agentd shows no in-flight request | SIGINT → finalize → exit 10 `cli_hang` | "Agent stalled" |
| Repo instruction file loaded despite policy | InstructionsLoaded hook | exit 12 | operator alert (CLI regression) |
| Agent deletes `/replicant/out` contents | finalize recomputes outputs from the workspace | no effect | — |
| Agent kills the entrypoint (same uid) | container exits non-zero without `outputs.json` | uploader writes a partial manifest; exit code ≠ 0 ⇒ `AgentExitedNonZero` | "Agent run crashed" |
| agentd crashes | native sidecar restarts (`restartPolicy: Always`); in-flight CLI requests fail with connection errors and the CLI retries | transient | none unless > `unavailableAfterSeconds` |
| Gateway 402 mid-run | agentd → IPC | exit 20 | "Run budget exhausted"; workflow may open `budget_extension` gate |

---

### 4.10 Role `qa`: the QA harness branch (R1-073, XD-05-17, XD-05-19)

The `qa-runner` agent definition ([examples/agent-qa-runner.yaml](examples/agent-qa-runner.yaml)) is the model profile
for the harness's agent sub-tasks (smoke-flow generation, diagnosis). It pins the CLI image `replicant/cli-claude-code`
(XD-FIX-R1-256 names; there is no separate `cli-qa-runner` image, XD-FIX-R1-045 21:45Z correction), which also ships
`/opt/replicant/bin/{replicant-qa,replicant-stubgen,replicant-redact,replicant-svc-wrap}` and `@replicant/qa-web` under
`/opt/replicant/lib/node_modules` (WS-05 owns them; `replicant-redact` carries WS-08's ruleset). They are built into the
image by the CLI image pipeline from WS-05's release (label `replicant.example.com/replicant-qa-version`) and copied into
`tools` by `prepare` like the entrypoint. Wrapped QA services run `/opt/replicant/bin/replicant-svc-wrap` from the
`tools` volume (XD-FIX-R1-045 21:45Z correction) in their `replicant/svc-<name>@sha256` images (XD-05-20). With `REPLICANT_ROLE=qa`,
`replicant-entrypoint run`:

1. runs prepare's results check, renders `$HOME` as for any Claude Code run (the harness may launch the CLI for agent
   tasks), and does **not** run environment bootstrap steps (the harness owns install/build stages);
2. execs `/opt/replicant/bin/replicant-qa run --inputs /replicant/inputs --out /replicant/out/qa --plan /replicant/inputs/run-plan.json`
   (WS-05 stage-design.md §4.4 row 8, XD-FIX-R1-073; the harness reads `/replicant/inputs/qa/config.json` itself)
   as a child (same uid, no credentials; model traffic, if any, through agentd `:7800`). Retry checkpoints of a
   previous run arrive as `place: qa_file` at `/replicant/inputs/qa/checkpoints/<stage>.json` (XD-W2-05-04); the
   harness writes its checkpoints to `/replicant/out/qa-checkpoints/<stage>.json`, which the uploader streams as
   `qa_report/checkpoints/<stage>.json` (XD-W2-02-08 supersedes the earlier `/replicant/spool/qa/` path);
3. when `qaHarness.agentTasks` is true and the harness requests an agent task, it calls back
   `replicant-entrypoint agent-task --task <name> --prompt-file <file>`, which launches Claude Code through the normal
   adapter (§2) with `--no-session-persistence`, the qa-runner tool policy (Read, Grep, Glob; Bash limited to
   `replicant-qa agent-*` subcommands; edits only under `/workspace/.qa/generated`), and the run-level turn and budget
   limits shared across all agent tasks of the run;
4. on harness exit reads `/replicant/out/qa/result.json` (`{verdict, exit_class, role_result}`, WS-05
   `replicant.role-result.qa/v1`) and `/replicant/out/qa/outputs.entries.json` (entries in this schema's entry shape with
   `producer: replicant-qa`), validates every entry (type in the C-ART QA set; path under `/replicant/out/qa/`, or under
   `/replicant/out/qa-checkpoints/` for `{type: qa_report, producer: replicant-qa}` checkpoint entries published as
   `qa_report/checkpoints/<stage>.json`, XD-W2-05-04; size within the C-ART maximum), writes `run_result` with that
   `role_result`, merges the entries into `outputs.json`, and exits per the harness exit class:

| Harness exit class | Entrypoint exit | `run_result` written |
|---|---|---|
| `pass`, `pass_with_warnings`, `fail`, `inconclusive_other`, `not_applicable` | 0 | yes |
| `environment_setup` (prepare, install, build tooling) | 44 (`EnvironmentSetupFailed`) | yes, `verdict: inconclusive`, `agentrun_exit_class: environment_setup` |
| `environment_services` (services, stubs, seed) | 45 (`EnvironmentSetupFailed`) | yes, `verdict: inconclusive`, `agentrun_exit_class: environment_services` |
| harness crash, or no `result.json` | 10 / 11 | no |
| disk / OOM | 30 / 31 | partial |

`run_result.outcome` follows the QA verdict (XD-FIX-R1-116 erratum; XD-W2-05-01 ruling: QA never uses `blocked`,
which the frozen schema reserves for clarification):

| `role_result.verdict` | `run_result.outcome` |
|---|---|
| `pass`, `pass_with_warnings` | `completed` |
| `not_applicable` | `no_changes` |
| `fail` | `failed` |
| `inconclusive` (all three inconclusive exit classes, including 44 and 45) | `failed` |

Consumers (WS-01 QaRun, WS-07 packet builder, WS-06 metrics) read `role_result.verdict`, never `outcome`. Service
sidecars for role `qa` have no `startupProbe` (XD-05-14, XD-02-15); the harness waits for readiness itself.

## 5. `replicant-agentd`

### 5.1 Responsibilities

1. **Token custody (per-audience token set, CCR-09-01; run grants, CCR-09-02; `acting_for`, CCR-09-03).** Reads
   the projected token set `/run/replicant/token-seed/<audience>` (`REPLICANT_RUN_TOKEN_SEED_DIR`; `ai-gateway`, `memory-mcp`, `context-mcp`,
   `repo-mirror`, `runner-sandbox`; only those the run grant lists). For each token it checks `aud` is exactly that
   audience (single string), `run_id`/`tenant_id`/`job_id`/`acting_for` are identical across the set, and the
   embedded `grant_jti` is the same in all. It refreshes all of them with one `POST /sandbox/v1/token:refresh`
   (presenting the `runner-sandbox` token) when the earliest expiry is fewer than 15 minutes away; the response is
   the token map `{"tokens": {"<audience>": {"token", "expires_at"}}}`. At start agentd copies the projected Secret files
   into its private tmpfs `agentd-state` (`/run/replicant/tokens/<audience>`, mode 0400, uid 10000) and runs with
   `REPLICANT_RUN_TOKEN_DIR=/run/replicant/tokens` (C-IDS 2.0.0 §8, CCR-02-08); every refresh rewrites those files
   atomically, which is the C-RUNNER "rewrites `$REPLICANT_RUN_TOKEN_DIR/<audience>`" obligation, fulfilled by agentd
   (the only reader). The read-only Secret mount at `/run/replicant/token-seed` keeps the initial set and is never read
   after start (R1-092; wording erratum CCR-03-08). **No token, of any audience, ever enters the `agent` container** (control-review G-01 confirms
   this model; WS-02 and WS-05 align their pod and QA designs to it). The QA harness (WS-05) uses the same loopback
   routes as the CLI.
1a. **Route → audience binding.** agentd injects exactly one token per route and never forwards a token to another
   audience:

   | Loopback route | Upstream | Token audience | Required scope (in token and grant) |
   |---|---|---|---|
   | `:7800/anthropic/*`, `/openai/*`, `/bedrock/*`, `/vertex/*` | replicant-gateway-shim | `ai-gateway` | `gateway:invoke` |
   | `:7801/mcp/memory` | memory-mcp `https://memory-mcp.<env>.replicant.internal/mcp` (`endpoints.memory_mcp`) | `memory-mcp` | `memory:read`, `memory:search` |
   | `:7801/mcp/context` | context-mcp `https://context-mcp.<env>.replicant.internal/mcp` (briefing, catalog, contracts, consumers, dependency graph, owners; `endpoints.context_mcp`) | `context-mcp` | `context:read` |
   | `:7801/mcp/code_search` | context-mcp `https://context-mcp.<env>.replicant.internal/mcp/code-search` (`code_search`, `find_call_sites`, `file_get`; `endpoints.code_search_mcp`) | `context-mcp` | `code_search:read` |
   | `:7802/git/<repo_id>` (prepare; afterwards read-only lazy blob fetches only) | repo-mirror | `repo-mirror` | `repo:fetch` |
   | `:7803/inputs/*` (prepare only) | S3 via `GET /sandbox/v1/credentials/aws` | `runner-sandbox` | `credentials:aws` |
   | (no loopback route) steering, refresh | runner-sandbox listener | `runner-sandbox` | `inbox:read` |
   | `uploader-ipc` `GET /v1/aws-credentials` (uploader only) | `GET /sandbox/v1/credentials/aws` | `runner-sandbox` | `credentials:aws` |

   A route whose audience is absent from the token set is closed (the CLI sees 404), so an agent definition can only
   reach services its run grant authorizes. The route mapping is WS-04's (XD-W2-04-04); profile `review-readonly`
   configures no `memory` server, so reviewers get no `/mcp/memory` route.
2. **Loopback proxy** (§1.3). For every request: drop inbound `Authorization`, `x-api-key`, `Proxy-Authorization`,
   and any `X-Replicant-*`; set `Authorization: Bearer <current token for the route's audience>`, `X-Replicant-Tenant-Id`, `X-Replicant-Job-Id`,
   `X-Replicant-Run-Id`, `X-Replicant-Role`, and on its own retries `X-Replicant-Retry-Attempt` (≥ 1, C-IDS §7); keep or
   create `traceparent` and emit an `agentd.proxy` span per request (XD-08-03; trace id per C-IDS T-1); enforce a
   route allowlist from the plan (only the configured protocol paths and MCP services); enforce `maxModelCalls`,
   `maxInputTokens`, `maxOutputTokens` locally; count model turns for the run-level turn budget (§2.6).
3. **Usage accounting.** Parses `usage` from JSON responses and from the final SSE events (`message_delta` /
   `message_stop` for Anthropic; `response.completed` for OpenAI Responses) without buffering the stream; reads
   `X-Replicant-Budget-*` headers; exposes totals over IPC. The shim, not agentd, is the authority when usage cannot be
   parsed (gateway.md §6.2a).
4. **Retry policy** for 429/529/502/503/504 (gateway.md §6), so CLI-visible errors are rare and uniform across CLIs.
5. **Steering relay** (§2.6) and inputs/git relay during prepare.
6. **Close**: on `finished` (or SIGTERM to the pod) it calls `POST /v1/runs/self:close` on the shim.
7. **Heartbeat and self-stop (XD-FIX-R2-030, XD-FIX-R2-040, XD-W2-02-07; CCR-02-12 accepted).** Every 30 s agentd calls
   `POST /sandbox/v1/heartbeat` (sandbox listener `:9443`, `runner-sandbox` token) with body
   `{"disk": {"state": "ok"|"warn"|"exceeded", "used_bytes", "limit_bytes", "peak_bytes"}}` read from the WS-02
   watchdog status files (`/run/replicant/ipc/status/disk-{state,used,limit,peak}`; the body is omitted while the files
   are absent). The response is `{"continue": bool, "cancel_reason": "<reason>"|null}`: a non-null `cancel_reason`
   raises IPC `cancel_requested {reason}`; `continue: false` raises it with reason `revoked`. After **120 s** without
   a successful heartbeat agentd raises IPC `heartbeat_lost` (the entrypoint stops the agent and finalizes, §4.5) and
   stops proxying model calls; the uploader then finalizes normally.
8. **Logs.** agentd honours bootstrap `endpoints.log_transport` (`stdout` | `otlp`) exactly like the entrypoint
   (§4.2), with attribute `replicant.container = agentd`.
9. **Debug (test images only, XD-FIX-R1-254).** `replicant-agentd debug tokens --claims-only` prints the decoded claims
   (never signatures or the encoded token) of every file in `$REPLICANT_RUN_TOKEN_DIR`. The subcommand is compiled only
   with build tag `replicanttest`, which only the `contract-probe` image uses; CI asserts on every other image's SBOM
   and symbol table that it is absent.

### 5.2 Why a sidecar and not CLI-native token wiring

Claude Code supports `apiKeyHelper` and Codex supports `model_providers.<id>.auth.command` for rotating tokens, but
both run the helper **as the agent's uid in the agent's container**, so the helper (and therefore the token) is
readable by the agent. A token in the agent container can be replayed from another compromised sandbox against
memory-mcp of this tenant before it expires. Keeping it in a separate container removes that path and gives one
uniform place for attribution headers, retries, and usage counting for both CLIs. Full analysis:
[ADR-006](adr/006-loopback-auth-proxy.md).

### 5.3 IPC API (`unix:/run/replicant/ipc/agentd.sock`, HTTP/1.1, JSON)

| Method | Path | Caller | Purpose |
|---|---|---|---|
| POST | `/v1/status/{prepared,agent-started,finalizing,finished}` | prepare / entrypoint | Phase reports (closes git/inputs routes at `prepared`) |
| GET | `/v1/events?after=<seq>&wait=20` | entrypoint | Long-poll: `budget_soft_limit`, `budget_exceeded`, `gateway_unavailable`, `gateway_recovered`, `steering_available`, `token_refresh_failed`, `cancel_requested {reason}`, `heartbeat_lost` |
| GET | `/v1/steering/next` | entrypoint | Next undelivered steering message |
| POST | `/v1/steering/{message_id}/{delivered,rejected}` | entrypoint | Ack (agentd acks upstream) |
| GET | `/v1/usage` | entrypoint | Counters for `run_result.usage` and `role_result.budget` |
| POST | `/v1/report/instructions` | hook | Instruction files loaded (reporting only) |
| GET | `/v1/turns` | entrypoint | agentd's model-call count for the run-level turn budget cross-check |
| POST | `/v1/heartbeat:now` | entrypoint (on SIGTERM) | Immediate heartbeat; returns `{cancel_reason}` from the response (≤ 3 s, else `null`) |

On the separate `uploader-ipc` socket (`/run/replicant/uploader-ipc/agentd.sock`, `REPLICANT_UPLOADER_SOCKET`; mounted only
in agentd and the uploader, XD-02-01):

| Method | Path | Caller | Purpose |
|---|---|---|---|
| GET | `/v1/aws-credentials` | uploader | AWS `credential_process` JSON `{"Version":1,"AccessKeyId","SecretAccessKey","SessionToken","Expiration"}` from `GET /sandbox/v1/credentials/aws` (agentd's `runner-sandbox` token), cached and refreshed 5 min before expiry; refused (409) after the uploader has written the manifest (XD-FIX-R1-042) |
| POST | `/v1/redact-known` | uploader | Body `{"text":"<utf8>"}` → `{"text":"<redacted>","hits":<int>}`: replaces the raw, base64 (std and url), percent-escaped, and JSON-escaped forms of every token and STS secret agentd holds or held during the run with `[REDACTED:known-credential:<fp12>]` (`fp12` = first 12 hex of sha256 of the secret); body ≤ 8 MiB (XD-FIX-R1-042) |

Every request on `agentd.sock` is accepted from any local client, because the agent can reach that socket. No endpoint returns or
accepts a credential, and the effects an agent could cause (faking a phase report, acking its own steering early)
only affect its own run.

---

## 6. Must-answer: how a reviewer runs with read-only intent while still executing tests

Read-only intent is enforced by **what can leave the sandbox and what is believed**, not by making the workspace
immutable (tests must write build outputs, `node_modules`, `.next/`, coverage files).

1. **Outputs are fixed by contract.** Reviewer agent definitions must set `outputContract.outputs.diff_bundle:
   forbidden` (schema-enforced for planner/reviewer roles). The entrypoint never runs diff collection for them, and the
   uploader rejects any `diff_bundle` entry from a run whose role is `reviewer` or `planner` (XD-03-05). Nothing the
   reviewer changes can reach the Publisher (D-21).
2. **Edits are removed from the tool surface where the CLI allows it.** Claude Code: `tools.edit: false` renders
   `Edit`, `Write`, `NotebookEdit` as **deny** rules in managed settings (deny wins in every mode), plus
   `Bash` deny patterns such as `git commit:*`, `git checkout:*`, `git reset:*`. Codex cannot remove its patch tool in
   exec; the instruction forbids edits.
3. **Integrity check (the authoritative control).** `workspaceIntegrity: enforce` (required for reviewers by lint):
   before the CLI starts, the entrypoint records `tree_before = git write-tree` over a temporary index of all tracked
   files (with the hardened git environment of §4.8); after `kill(-1)` it computes `tree_after` the same way and the
   list of modified tracked paths. Untracked and ignored files (build outputs) are not part of the comparison. Any
   difference ⇒ `role_result.integrity.tracked_files_modified = true`, outcome `failed`, exit 12 (`enforce`) or a
   flagged result (`detect`). The verifier node discards a review whose integrity is not clean.
4. **Optional tripwire.** `plan.workspace.protect_tracked_files: true` makes prepare `chmod a-w` every tracked file,
   so accidental edits fail loudly inside the run instead of being found afterwards. It is not a security control
   (the agent owns the files and can `chmod` them back); the integrity check is.
5. **The reviewer's test results are advisory.** The QA stage (WS-05) runs on a fresh checkout plus the diff bundle
   and is the authority on whether tests pass; the reviewer's `tests_run` are evidence for its own findings only.

---

## 7. Must-answer: how a CLI upgrade is rolled out, evaluated, and rolled back

A CLI upgrade is a **configuration** change (CC-1): a new CLI image digest in a new AgentDefinition version, shipped
in a new Release Bundle. Because the entrypoint, agentd, and the CLI adapter are in the same image, an upgrade that
changes flags or event formats ships its adapter changes in the same digest.

1. **Build** (CLI image repository, GitHub Actions): Renovate opens a PR bumping `CLAUDE_CODE_VERSION=2.1.311` (npm
   `@anthropic-ai/claude-code-linux-x64` and `-musl` tarballs, verified against the npm registry `dist.integrity`
   SHA-512) or `CODEX_VERSION` (`@openai/codex@<v>-linux-x64` tarball). The Dockerfile is `FROM scratch`-style
   (`distroless/static`) with `/opt/replicant/cli/<cli>/<version>/`, `/opt/replicant/bin/*`, and
   `/opt/replicant/etc/gitconfig`. Labels: `org.opencontainers.image.version=<cli version>`,
   `replicant.example.com/entrypoint-version`, `replicant.example.com/cli=<cli>`.
2. **Adapter contract tests** (`replicant-entrypoint contract-test`, in CI, against the real binary and the gateway
   stub in `script` mode): every flag in §2.1/§3.1 is accepted; `system/init` reports `dontAsk`, the expected tools,
   no `WebFetch`/`WebSearch`; MCP loopback servers connect; a planted `/workspace/repo/CLAUDE.md`, `AGENTS.md`,
   `.claude/settings.json` with a hook, and `.mcp.json` are **not** loaded (InstructionsLoaded hook and init event);
   `--json-schema`/`--output-schema` produce schema-valid output; stream-json steering injection produces a second
   turn; resume of a session captured by the previous pinned version works or fails with exit 42 (both outcomes are
   recorded; the second informs WS-04); exit codes for a scripted 402, 429-then-200, and persistent 503; the run-level turn budget holds across ten scripted steering messages (R2-043); `--system-prompt-snapshot off` is accepted on resume (R2-053); `managed-mcp.json` loads with plain loopback URLs (U-10); the QA branch maps every harness exit class (§4.10). Recorded
   CLI event fixtures are replayed through the parser (backward compatibility).
3. **Context-overhead calibration (XD-04-02):** CI runs the CLI against the shim in `record` mode with an empty task and
   measures the CLI's own system prompt and tool definitions with `count_tokens` per model in the agent definitions
   that pin this image; the result is published as image label `replicant.example.com/cli-overhead-tokens`
   (`{"<gateway model id>": <tokens>}`), which WS-04's context budget reads by image digest.
4. **Supply chain:** image signed with cosign (KMS key), SBOM and SLSA provenance attestations
   (environment-definition.md §7.4); pushed to ECR repository `replicant/cli-<cli>` with tag immutability.
5. **Definition change:** a bot PR in the config repo updates `cliImage.image`/`version`/`entrypointVersion` in the
   affected AgentDefinitions; config CI runs schema validation, the materialization dry-run, and a CLI smoke run
   against the stub (config-publishing.md §3). Merge publishes new definition versions.
6. **Bundle and evaluation** (WS-06): a bundle request with the new agent versions builds a candidate bundle whose
   only diff is `cli_images` and `agents`. WS-06 runs the eval tier for CLI changes: the golden task suite per
   codebase type, compared with the parent bundle on task success rate, review findings, QA pass rate, cost per
   task, turns, wall time, `AgentExitedNonZero`/`AgentResultInvalid` rate, and `SessionRestoreFailed` rate.
   Promotion requires no regression beyond WS-06 thresholds.
7. **Ramp** through bundle assignment (C-DEF §4.5): `replicant-eval` → 5% of jobs → selected tenants → 100%, watching
   the per-bundle dashboards (WS-08) for the same metrics on live traffic.
8. **Rollback:** reassign the previous bundle (takes effect for new jobs immediately). Running jobs keep the CLI of
   the bundle they were pinned to, so they are unaffected; their later runs also use the old image. If the new CLI is
   harmful to running jobs, operators revoke the bundle (`release.notice(bundle_revoked)`) and offer migration back.
9. **Retention (R1-085, XD-06-14, XD-FIX-R1-147 as ruled):** images are kept by per-bundle tags
   `bundle-ref-<first 20 chars of rb_ id body>`. `svc_release-controller` (WS-06) applies the tag to every
   `replicant/cli-*` and `replicant/env-*` image in a bundle's closure **at bundle build** (WS-06 release-bundle.md §4, upgrades.md §8, R1V-112 ruling;
   derived bundles add no tags) (`ecr:PutImage` of the existing manifest, with `ecr:BatchGetImage`; tag immutability forbids only overwriting an
   existing tag, so adding a new tag works), and `sys:retention-sweeper` removes it (`ecr:BatchDeleteImage` of the tag)
   when the bundle is revoked or retired (WS-06 upgrades.md §8). WS-10's lifecycle policy keeps every image with
   `tagPrefixList: ["bundle-ref-"]` and expires untagged images after 14 days. CI build tags (`bs-<hex12>`,
   `<cli version>-ep<entrypoint version>`) carry no retention meaning. Old bundles therefore always roll back.
10. **Session compatibility across versions:** a job migrated to a bundle with another CLI never resumes a session
   (SR-1 condition 6, §2.8); it continues fresh with the handoff.

---

## 8. Local mode (D-20)

`runner-local` (WS-02/WS-11) runs the same three images (CLI image for agentd and prepare, environment image for
the agent) with Docker or Podman, the same volume layout, and the same uids. Differences: agentd forwards to the local
`replicant-gateway-shim` (`http://gateway-shim:8420`, gateway.md §8), MCP to local memory/context services, inputs to
the local S3 store; the run token is signed by runner-local's dev key and the run grant by the local grants host
(`replicant-local-core`, XD-FIX-R1-253). `replicant dev run-agent --plan <manifest.json>` runs a single agent container
against a local plan for prompt development.

Local image names (XD-FIX-R1-256; registry prefix `localhost/replicant/`, tag `dev`): `cli-claude-code` and `cli-codex`
(each carries `/opt/replicant/bin/replicant-entrypoint` and `replicant-agentd`; there is no separate `sandbox-entrypoint`
image), `env-<name>` for environment images (`env-go-service`, `env-python-service`, `env-node-service`,
`env-nextjs-app`, `env-docs-site`, `env-web-frontend`), `contract-probe` (the test variant with
`replicant-agentd debug tokens --claims-only`, §5.1 item 9), and `replicant-gateway-shim`. Production repositories use the
same names under `replicant/`.

---

## 9. Unverified items (each has a fallback; none is load-bearing without it)

| ID | Claim | Why unverified | Fallback |
|---|---|---|---|
| U-2 | `--setting-sources user` alone prevents loading the repo's `./CLAUDE.md` | Docs state it only for project rules | `instructionFiles: managed-only` (verified) + `claudeMdExcludes` + InstructionsLoaded audit + CI contract test |
| U-3 | `CLAUDE_CONFIG_DIR` relocates `~/.claude.json` to `$CLAUDE_CONFIG_DIR/.claude.json` | Only code evidence in the 2.1.310 binary | `HOME=/home/agent` makes both locations inside the `home` volume |
| U-4 | `AWS_REGION` needed with `CLAUDE_CODE_SKIP_BEDROCK_AUTH`; `CLOUD_ML_REGION` semantics with Vertex | Not checked (`CLOUD_ML_REGION` exists only as a string in the binary) | Set both anyway (harmless); Bedrock/Vertex protocols are optional |
| U-5 | `codex exec` option placement around `resume` | Not checked | Contract test pins the working form per CLI version |
| U-6 | bwrap (`workspace-write`) works under gVisor/Kata with `drop: [ALL]` | Not documented upstream | Default `danger-full-access` |
| U-7 | cgroup v2 `memory.peak` readable inside gVisor and Kata guests | gVisor cgroupfs emulation and Kata guest cgroups not checked; gVisor issue #13067 shows host cAdvisor gaps | Watchdog-sampled `memory.current` maximum; kubelet `/stats/summary` |
| U-8 | Downward API annotation updates propagate into running Kata/gVisor pods | Not checked | Takeover uses a dedicated `takeover` role run that restores diff and session and archives (role exists in C-RUNNER) |
| U-9 | Claude Code reads managed settings changes live; agent cannot affect them (they are read-only mounts) | Not needed | — |
| U-11 | `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` disables CLAUDE.md loading | Name present only in the 2.1.310 binary, not documented | Not relied on: `instructionFiles: managed-only`, `claudeMdExcludes`, and the InstructionsLoaded audit are the controls |
| U-13 | Codex follows a symlinked `$CODEX_HOME/AGENTS.md` (to the tmpfs joined prompt, §1.4) | Not documented; Codex reads the file through the filesystem, which follows symlinks unless it opens with `O_NOFOLLOW` | Adapter contract test asserts the instructions arrive; fallback: a regular 0444 copy in `$CODEX_HOME`, which no capture or archive includes |
| U-14 | Whether Codex rollout files record the global `AGENTS.md` text (session metadata) | Not documented for 0.164.0 | The capture step strips the memory block bytes from session files regardless (§2.8) |
| U-12 | Lazy blob fetches through the loopback git route stay within the 20 requests/min budget for normal agent git use | Not measured | Raise the limit per environment; the route only serves granted repos read-only |
| U-10 | `managed-mcp.json` accepts plain `http://127.0.0.1` URLs (the docs restrict `http://` only for `managedMcpServers`) | Not stated for `managed-mcp.json` | agentd also serves the MCP routes over TLS on `https://127.0.0.1:7811` with a pod-local CA (generated by prepare, trusted through `NODE_EXTRA_CA_CERTS`); the adapter contract test picks the working form per CLI version |

## Open questions

Closed in the fix round: repair session resume follows SR-1 (§2.8), aligned with WS-04 ADR-04-001 (R1-093).

| Question | Who decides | Options | Recommendation |
|---|---|---|---|
| Codex live steering via app-server once supported for production | WS-03, WS-06 | stay on exec / switch | Re-evaluate on the first Codex release that drops the "experimental" label (ADR-003) |
| Native sidecar ordering guarantees under Kata for `prepare` → agentd dependency | WS-02 | native sidecar / regular container + wait loop | Native sidecar with startupProbe (KEP-753 behavior); WS-02 verifies |

## Risks

- CLI behavior drifts between versions (flags, instruction-file loading, default permission mode — 2.1.302 already
  changed the `-p` default). Mitigation: the adapter contract tests in the CLI image CI are mandatory gates (§7).
- Claude Code `--max-budget-usd` uses Anthropic list prices; when the gateway prices differ, the CLI may stop early
  (`error_max_budget_usd`) or late. The gateway is authoritative; the CLI flag is set to the same budget and treated
  as an early stop only.
- The entrypoint shares uid with the agent. The design does not rely on it being uncompromised between bootstrap and
  `kill(-1)`, but a sophisticated agent could still corrupt its own reporting (summary, transcript). Reports are
  labeled untrusted end to end.

## Interfaces consumed / provided

- **Consumed:** C-RUNNER (`/sandbox/v1/token:refresh`, `/sandbox/v1/credentials/aws`, `/sandbox/v1/messages`,
  `:ack`, `RunTokenClaims`, run-result envelope), C-CRD (phases, failure reasons, child objects, `timeouts`), C-ART
  (artifact types, run prefixes, `inputs/`, streaming upload cadence), C-IDS §5/§7 (trace rule, env vars, headers),
  C-DEF §6 (entrypoint reads only materialized config).
- **Provided:** `replicant.run-inputs/v1` (schemas/run-inputs.schema.json), `replicant.run-outputs/v1`
  (schemas/run-outputs.schema.json), role results `replicant.role-result.{implementer,planner,reviewer,takeover}/v1`,
  `replicant.transcript/v1`, `replicant.sandbox-bootstrap/v1`, the exit-code contract (§4.6), the agentd IPC API (§5.3).
