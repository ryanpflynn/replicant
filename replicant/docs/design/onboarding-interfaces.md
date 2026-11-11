# Repo onboarding: interface contract (ONBOARD-01)

These are the shared shapes for design doc §8c, so the Go, services, worker and web workstreams agree. Add to this
document; don't change a shape here without updating every producer and consumer.

## 1. Repo profile (github-integration → control-plane-api)

`PUT /internal/v1/repos/{repo_id}/profile`. Auth is a bearer token: the same internal secret github-integration
already uses for control-plane-api internal calls. The call is idempotent per `(repo_id, sha)`.

```json
{
  "repo_id": "R_...", "full_name": "acme-payments/billing-py", "default_branch": "main", "sha": "<40 hex>",
  "scanned_at": "2026-11-10T12:00:00Z", "scanner_version": 1,
  "files": [{"path": "CLAUDE.md", "sha256": "...", "bytes": 812}],
  "ecosystems": [{"ecosystem": "python", "version": "3.12", "package_manager": "uv", "source": ".python-version"}],
  "ci_commands": [{"stage": "test", "command": "uv run pytest -q", "source": ".github/workflows/ci.yml#jobs.test"}],
  "devcontainer": {"image": "...", "features": []},
  "claude": {
    "settings_present": true,
    "hooks": [{"event": "PostToolUse", "matcher": "Edit|Write", "command": "uv run ruff format $FILE", "command_hash": "sha256:..."}],
    "rules": ["python-style.md"], "skills": ["migrations"], "commands": [], "agents": [],
    "claude_md": ["CLAUDE.md", "src/billing/CLAUDE.md"],
    "permission_rules": 3
  },
  "agents_md": true, "copilot_instructions": false,
  "mcp_servers": [{"name": "schema", "transport": "stdio", "command": "npx", "args": ["-y", "@acme/schema-mcp@1.2.0"],
                   "url": null, "env_keys": [], "header_keys": [], "identity": "stdio:npx:@acme/schema-mcp@1.2.0"}],
  "pre_commit": {"present": true, "hooks": ["ruff", "end-of-file-fixer"]},
  "errors": [{"path": ".mcp.json", "error": "invalid JSON"}]
}
```

- **What's sent:** only names, hashes and env or header **keys**. File contents and env values are never sent.
- **The response:**
  - `200 {"profile_id": "rp_...", "environment": {...}, "classified": [...]}`
  - `environment` is the inference result (§2), computed by control-plane-api.

## 2. Environment inference (control-plane-api `inventory.infer_environment`)

```json
{"environment": "python-service", "version": "3.12",
 "commands": {"install": "uv sync --frozen", "build": null, "test": "uv run pytest -q", "lint": "uv run ruff check ."},
 "source": {"install": "default:uv", "test": ".github/workflows/ci.yml#jobs.test", "lint": ".github/workflows/ci.yml#jobs.lint"},
 "supported": true, "unsupported_reason": null}
```

- **Environments:** `go-service`, `python-service` or `node-service`.
- **Priority:** CI commands, then devcontainer and version files, then manifests and lockfiles, then defaults.
- **Default commands:**
  - Go: `go mod download` / `go build ./...` / `go test -count=1 ./...`
  - uv: `uv sync --frozen` / – / `uv run pytest -q`
  - pip: `pip install -r requirements.txt` / – / `python -m pytest -q`
  - Node: `<pm> install --frozen-lockfile`, or `npm ci` / `<pm> run build` if the script exists / `<pm> test`
- **CI commands that are dropped:** anything that's clearly not a build or test step, such as deploy, docker,
  release, upload or checkout actions.

## 3. Catalogue classes

| kind | identity | classes |
|---|---|---|
| `mcp_server` | `stdio:<command>:<pinned pkg>` or `url:<origin>` | `auto_allow`, `map`, `needs_egress`, `needs_credentials`, `review` |
| `hook` | `hook:<event>:<command_hash>` | `ok`, `at_risk` (network tool, or a binary not in the environment) |
| `toolchain` | `toolchain:<ecosystem>:<version>:<pm>` | `supported`, `unsupported` |
| `probe_failure` | `probe:<environment>:<cause>:<detail>` | `missing_tool`, `network`, `timeout`, `test_failure`, `build_failure`, `install_failure` |

- **Decisions** (by admins) can be `approve` (→ allowed), `deny` or `map:<platform service>`, and override the class
  default.
- **`auto_allow`** requires all of: stdio transport, an exact package version, no `env_keys` or `header_keys` that
  look like secrets, and no URL.

## 4. Job request field (control-plane-api intake → worker), pinned per job

`JobRequest.repo_profile`, optional; absent means the old behaviour (go-service, quote_as_context):

```json
{"profile_id": "rp_...", "sha": "<default-branch sha>", "environment": "python-service",
 "commands": {"install": "...", "build": null, "test": "...", "lint": "..."},
 "repo_config": "use", "config_base_sha": "<default-branch sha>",
 "mcp_auto_allow": [{"name": "schema", "command": "npx", "args": ["-y", "@acme/schema-mcp@1.2.0"]}],
 "readiness": "ready"}
```

`repo_config` comes from the project's pinned config, and is `ignore` when it is set that way. Readiness that is
`blocked`, without `triggers.readiness_override: true`, gets a 409 `repo_not_ready` at intake. A job labelled
`replicant-readiness` is the exception.

## 5. Run plan additions (`run-plan.json`, the Go `RunPlan`)

- **`environment.commands`:** `{install, build, test, lint}`. Strings or null. `replicant-qa` runs these when they
  are present; otherwise the Go fallback.
- **`repo_config`:** `"use" | "ignore"`. Defaults to `"ignore"` when absent, which is today's behaviour.
- **`config_base_sha`:** a string or null.
- **`mcp_auto_allow`:** `[{name, command, args}]`.
- **`agent.model.parameters.reasoningEffort`:** `low|medium|high|max`, optional. Rendered as `--effort` only if the
  pinned CLI supports it.
- **`repo_instructions`:** still `quote_as_context|ignore`. With `repo_config=use`, the quoting skips `CLAUDE.md`,
  which is loaded natively instead.

## 6. Run report additions (role result `cli` section and QA verdict)

```json
"repo_config": {"mode": "use", "config_base_sha": "...", "settings_hash": "sha256:...",
  "hooks": [{"event": "PostToolUse", "command_hash": "sha256:...", "runs": 4, "failures": 0, "cause": null}],
  "rules": ["python-style.md"], "skills": ["migrations"], "claude_md": ["CLAUDE.md", "src/billing/CLAUDE.md"],
  "mcp_skipped": [{"name": "jira", "identity": "url:https://jira.acme.internal", "reason": "not_allowed"}],
  "mcp_allowed": ["schema"], "permission_rules_ignored": 3, "hook_events_available": true}
```

- **`instruction_files_loaded`** is now filled in, from the InstructionsLoaded hook.
- **QA verdict:** each failed stage gets `cause` ∈ the probe_failure causes (§3) and `detail`, e.g. the missing
  binary's name.
- **Additions from the Go implementation** (all of them optional, and none changes an existing field):
  - `hooks[].command_hash` is `sha256:` + the hex sha256 of the hook's `command` string (UTF-8). The scanner must
    compute the same value, so `hook:<event>:<command_hash>` identities match.
  - `hooks[]` entries also carry `matcher`, `blocks` (exit-2 runs, which are the hook's own blocking or feedback signal
    and aren't counted as failures) and `detail` (e.g. the missing binary).
  - A hook's `cause` is `missing_tool`, `network`, `timeout` or `non_zero_exit`. Results that can't be attributed to
    one command are reported in an entry with `command_hash: null`.
  - `mcp_skipped[].reason` can also be `invalid` (an unparseable `.mcp.json`).
  - QA runs the `test` command as the stage `unit` and the `lint` command as a new stage `lint`. Each stage also
    records the `command` it ran.
  - A lint failure has the cause `lint_failure` (a seventh probe-failure cause). It is a warning
    (`pass_with_warnings`), not a failure.
  - The pinned CLI's `--effort` also accepts `xhigh`.

## 7. Observations (worker → control-plane-api)

`POST /internal/v1/inventory/observations`. Best effort; a failure never fails the job.

```json
{"job_id": "job_...", "run_id": "run_...", "repo_id": "R_...", "workflow_type": "bugfix", "role": "implementer",
 "items": [{"kind": "mcp_server", "identity": "url:https://jira.acme.internal", "status": "skipped", "cause": "not_allowed"},
           {"kind": "hook", "identity": "hook:PostToolUse:sha256:...", "status": "loaded", "cause": null},
           {"kind": "probe_failure", "identity": "probe:node-service:missing_tool:libvips", "status": "failed", "cause": "missing_tool"}],
 "tier": "standard", "model": "anthropic/claude-sonnet-5-5", "settings_hash": "sha256:..."}
```

For readiness jobs, the worker also calls `PUT /internal/v1/repos/{repo_id}/readiness` with
`{"job_id", "profile_id", "status": "ready|warnings|blocked", "causes": [{"stage", "cause", "detail"}]}`.

## 8. Project config additions

- **Pinned:**
  - `repo_config: "use" | "ignore"`, default `"use"`.
  - `models: {"default_tier": "standard", "workflows": {"bugfix": "thorough"}}`, default
    `{"default_tier": "standard", "workflows": {}}`.
- **Live:** `triggers.readiness_override: false`.
- **The derived bundle** gets a policy `model_tier_selection`:
  `{"default_tier": ..., "workflows": {...}, "escalation": {"repair_cycle_at_least": 2, "risk_at_least": "high", "after_failed_attempt": true}}`.

## 9. Model tier catalogue (platform policy `model_tiers` in the base bundle)

```json
{"tiers": {
  "standard": {"planner": {"model": "anthropic/claude-opus-5-5", "fallbacks": ["anthropic/claude-sonnet-5-5"], "thinkingTokens": 16000, "reasoningEffort": "high"},
               "implementer": {"model": "anthropic/claude-sonnet-5-5", "fallbacks": ["anthropic/claude-opus-5-5"], "thinkingTokens": 8000, "reasoningEffort": "medium"},
               "repair": "implementer",
               "reviewer": {"model": "anthropic/claude-opus-5-5", "fallbacks": ["anthropic/claude-sonnet-5-5"], "thinkingTokens": 16000, "reasoningEffort": "high"},
               "scanner": {"model": "anthropic/claude-sonnet-5-5", "thinkingTokens": 8000, "reasoningEffort": "medium"},
               "small": "anthropic/claude-haiku-5-5"},
  "thorough": {"planner": "standard.planner",
               "implementer": {"model": "anthropic/claude-opus-5-5", "fallbacks": ["anthropic/claude-sonnet-5-5"], "thinkingTokens": 32000, "reasoningEffort": "high"},
               "repair": "implementer", "reviewer": "standard.reviewer", "scanner": "standard.scanner",
               "small": "anthropic/claude-haiku-5-5"}}}
```

String values are references, either to another role in the same tier or to `<tier>.<role>`. The qa role runs
`replicant-qa` and uses no model.

## 10. Admin and project API (control-plane-api → web)

- **Admin endpoints** (require `replicant-admins`):
  - `GET /v1/admin/fleet/items?kind=&class=&sort=impact|repos|recent&limit=&cursor=` returns
    `{items: [{id, kind, identity, class, decision, effective, repos, jobs, impact: {score, jobs_with, jobs_without, delta: {qa_first_pass, rejected, repair_cycles, failed}}  (each delta is a ratio: rate with the item ÷ rate without it; 1 = no difference, null = not enough data), first_seen, last_seen, requests}], next}`.
  - `GET /v1/admin/fleet/items/{id}` returns the item plus `repos: [{repo_id, full_name, project_id}]`,
    `recent_jobs: [{job_id, status, outcome, at}]`, `decisions: [...]` and `requests: [...]`.
  - `POST /v1/admin/fleet/items/{id}:decide` takes `{decision: "approve|deny|map", map_to?, reason}` and returns 200
    with the item. It is audited.
  - `POST /v1/admin/fleet:rescan` takes `{repo_ids?: [...]}` and returns 202.
- **Project endpoints:**
  - `GET /v1/projects/{id}/readiness` returns
    `{repos: [{repo_id, full_name, status, profile_id, sha, environment: {...§2}, repo_config: {...profile.claude summary}, warnings: [{item_id, kind, identity, class, message}], last_probe: {job_id, status, at, causes}}]}`.
  - `POST /v1/projects/{id}/repos/{repo_id}:recheck` returns 202 `{job_id}`. It needs `tenant:policy:edit` or
    memory admin.
  - `POST /v1/projects/{id}/fleet-items/{item_id}:request` takes `{reason}` and returns 201. It needs
    `project_admin`.
- **UI capability:** `fleet:admin`, the same rule as `memory:admin`.

## 11. Additions from the services implementation

Additive only; every shape above still holds.

- **§1 profile:**
  - a `node` ecosystem entry from `package.json` also carries `scripts` (script **names** only), which §2 uses for
    the `build`, `test` and `lint` defaults;
  - `files[].sha256` is `null` for files that are listed but never read (lockfiles such as `uv.lock` and
    `pnpm-lock.yaml`); `bytes` comes from the tree.
- **§2 environment:** the stored inference also has `ecosystem`, `package_manager` and `requested_version`. When no
  supported ecosystem is found, `environment` is `null` and `supported` is `false`. Readiness is then `warnings` with
  cause `{stage: "environment", cause: "unsupported"}`, and no probe runs.
- **§4 `repo_profile`:**
  - `environment` may be `null` (see §2); the worker treats that as `go-service`, the old behaviour;
  - `readiness` is one of `pending | probing | ready | warnings | blocked`;
  - intake drops any `repo_profile` a submitter sends;
  - the field is in `job-request.schema.json` (all vendored copies).
- **§3:** `probe_failure` classes also include `lint_failure` (the QA `lint` stage). The scanner's
  `command_hash` is `"sha256:" + hex(sha256(exact command))`, the same as §6.
- **§7:** an observation for a job that intake doesn't know answers 404. Items the scanner never saw are added to the
  catalogue, classed from what happened (MCP server `review`; hook `ok`, or `at_risk` when it failed).
- **§10:**
  - readiness `warnings[]` also carries `effective` (`skip | warn | block | map:<x>`);
  - each repo also has `scanned_at` and `repo_config_mode` (`use | ignore`, from the project config);
  - `repo_config` also lists `mcp_servers [{name, identity, transport}]`, `agents_md` and the profile's `errors`;
  - `delta` ratios are add-half smoothed, `(x_with + 0.5)/(n_with + 1) ÷ (x_without + 0.5)/(n_without + 1)`, so a
    zero count stays finite. A delta is `null` when either side has no terminal jobs.
  - **Comparable jobs** are terminal, non-evaluation jobs on the same repo with the same workflow type that didn't
    observe the item.
  - **`score`** is `jobs_with × mean(ln ratio)`. The QA first-pass ratio is inverted, so a positive score means the
    item hurts. With no comparable jobs, the score is `jobs_with × the bad-outcome rate`. A `probe_failure` item adds
    one point per repo it blocks.
