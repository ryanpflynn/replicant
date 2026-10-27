# control-plane-api (CONTROL, M1)

FastAPI 0.162 / SQLAlchemy 2.1 (async, psycopg 3) / temporalio 1.34. Spec: `spec/07-control-plane-and-ux/architecture.md`
§2, `api/control-plane-api.openapi.yaml`, WS-06 `api/releases.openapi.yaml` (implemented subset below).

The image has two build targets (identity.md §9.3 L-1): `prod` (default) and `dev` = prod + `replicant-dev-auth`
(dev seed token, static dev users, `/v1/admin/dev/**`). The same image runs the API (`control-plane-api serve`, 8401)
and the run-grant app (`control-plane-api serve-grants --port 8403`, compose service `run-grants`).

## Routes in M1

| Route | Permission | Notes |
|---|---|---|
| `POST /v1/jobs` | `job:create` / `service:<adapter>` | C-JOBREQ validation, intake kill switch (Postgres record), submitter rule (R1-161), requester authz, per-PR cap, insert-or-get on `(tenant_id, dedupe_key)` + WS-06 `resolve()` in one transaction, then the **pinned** `ReplicantJob` start after commit; failed starts stay `pending` and the 5 s retrier restarts them |
| `GET /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/timeline` | `job:read` | from `jobs_read` (projector-owned); 404 when not visible; projection seq/lag headers |
| `GET /v1/approvals`, `GET /v1/gates/{id}` | signed in / `job:read` | from `approvals.gates` + `jobs_read.jobs.snapshot.gates` |
| `POST /v1/gates/{id}/decisions` | `gate:decide` | DecisionInput + C-MSG `GateDecideRequest` pre-checks (V5–V9 shape), `decision_id = derived("dec", ui\|cli, Idempotency-Key)`, outbox `intake.pending_actions` + Update `gate.decide` (10 s); 202 `{state: recording}` on timeout, 2 s retrier |
| `POST /internal/v1/gates/{id}/decisions` | `service:slack-integration\|github-integration` | adapter decisions |
| `PUT /v1/admin/platform-caps`, `GET` | `admin:platform\|dev:seed` | |
| `POST /v1/admin/tenants`, `PUT /v1/admin/tenants/{t}/settings` | `admin:platform\|dev:seed` | quotas default from caps (R2-055) |
| `PUT /v1/tenants/{t}/github/repos/{repo_id}/settings` | `tenant:ops:edit\|dev:seed` | forwarded to github-integration (writer of `github_app`) |
| `GET /v1/admin/migrations` | `admin:platform\|dev:seed` | from `ops.*` |
| `GET /v1/me`, `GET /v1/tenants` | signed in | for control-plane-web (`routes_web.py`); capabilities from the static role table |
| `GET /v1/jobs/{id}/runs`, `GET /v1/jobs/{id}/artifacts` | `job:read` | `jobs_read.job_runs` / `job_artifacts` (runs fall back to the snapshot) |
| `POST /v1/jobs/{id}:cancel\|:pause\|:resume`, `POST /v1/jobs/{id}/steering` | `job:control:own\|any` | Updates `job.*` through the outbox (Update ID = action_id / message_id) |
| `GET /v1/gates/{id}/packet/refs/{ref}` | `job:read` | same-origin stand-in for presigned packet links (WEB-02); `GET /v1/gates/{id}` inlines the packet's small JSON refs |
| `GET /v1/releases/bundles`, `GET /v1/releases/workers` | `release:read\|dev:seed` | read-only, from the registry and `jobs_read.v_pinned_counts` |
| `GET /v1/admin/kill-switches` | `ops:kill-switch` | read-only from `tenancy.kill_switches` (no `PUT` in M1) |
| `POST /v1/registry/publish`, `POST /v1/registry/bundles:build`, `GET /v1/registry/bundles/{rb}` | `registry:publish\|dev:seed` | M1 simplifications in `routes_registry.py` docstring |
| `GET /v1/releases/{env}/assignment`, `POST /v1/releases/{env}/assignment:bootstrap` | `release:read\|operate` / `dev:seed` | |
| `GET /internal/v1/tenants/{t}/gateway-quota` | `service:replicant-gateway-shim` | |
| `POST /internal/v1/notifications` | `service:replicant-worker` | notification fan-out (notifications.md N-1): recipients, preferences, quiet hours -> `notify.deliveries` (dedupe_key unique) + `notify.feed`; see Notifications below |
| `GET/PUT /v1/me/notification-preferences` | signed in | schema notification-preferences v1; `none` refused for obligations |
| `GET /v1/me/waiting` | signed in | v0 addition (NOTIFY-D4): gates you can decide + open PRs where you are requester/reviewer; UI "Waiting on you" |
| dev only: `GET/PUT /v1/admin/dev/seed-status[/{step}]`, `POST /v1/admin/dev/release-bundles` | `dev:seed` | bundle shortcut: `{worker_build_id?, spec?, bootstrap?}` |
| `serve-grants`: `GET /.well-known/jwks.json`, `GET /grants/revoked` | public | JWKS file (private members stripped); revocations from `jobs_read` + exec feed |

Authentication in M1 (no Dex/identity-service): `Authorization: Bearer <token>` from
`REPLICANT_INTERNAL_CALLER_TOKENS_FILE` (service callers), the dev seed token (`svc_dev-seeder`), or
`X-Replicant-Dev-User: <login>` for the static dev users (grace, carol, alice, bob, dave, erin; `replicant_dev_auth`). The
authorization evaluator (`authz.py`) has the identity-service `POST /internal/v1/authorize` request/decision shape and
runs in-process; repository permissions are not checked live in M1.

## Run and test

```sh
# unit + DB integration tests (uv env; DB tests need the ctl- environment)
cd replicant && UV_PROJECT_ENVIRONMENT=$PWD/.venv-ctl uv sync --package control-plane-api --package replicant-dev-auth
services/control-plane-api/tests/smoke/env.sh up          # ctl-pg (migrated by PLATFORM's migrate image), ctl-temporal
CTL_DB_DSN=postgresql+psycopg://svc_control_plane_api@127.0.0.1:55432/replicant \
CTL_DB_PASSWORD_FILE=$CTL_HOME/secrets/db_svc_control_plane_api \
CTL_SU_DSN=postgresql+psycopg://postgres:<pw>@127.0.0.1:55432/replicant \
  .venv-ctl/bin/python -m pytest -q services/control-plane-api/tests

# images
podman build -f services/control-plane-api/Dockerfile --target dev  -t localhost/replicant/control-plane-api:dev .
podman build -f services/control-plane-api/Dockerfile --target prod -t localhost/replicant/control-plane-api:prod .

# component smoke (all three CONTROL services + Gitea + Postgres + Temporal + stub ReplicantJob worker)
PYTHON=.venv-ctl/bin/python services/control-plane-api/tests/smoke/smoke.sh
services/control-plane-api/tests/smoke/smoke.sh down
```

Environment: exactly the spec compose names (`REPLICANT_HTTP_PORT`, `REPLICANT_DB_DSN`, `REPLICANT_DB_PASSWORD_FILE`,
`REPLICANT_TEMPORAL_*`, `REPLICANT_DEV_SEED_TOKEN_FILE`, `REPLICANT_INTERNAL_CALLER_TOKENS_FILE`, `REPLICANT_RUN_GRANT_JWKS_*`,
`REPLICANT_EXEC_REVOCATIONS_*`, `REPLICANT_S3_*`), plus `REPLICANT_WORKER_BUILD_ID` (dev bundle default),
`REPLICANT_GITHUB_INTEGRATION_URL` (default `http://github-integration:8080`), `REPLICANT_WORKER_READY_QUEUES`
(task queues a build must poll before the registrar marks it ready; default the three replicant-worker queues) and
`REPLICANT_CPA_BACKGROUND_TASKS` (`false` disables the retriers and the registrar, for tests).

## Notifications (v0, docs/v0/DEVIATIONS.md NOTIFY-*)

`notify.py` turns workflow intents into deliveries; `notify_render.py` holds the fixed templates; `notify_loops.py`
runs the dispatcher (pushes `slack_dm` rows to slack-integration and `github_comment` rows to github-integration
`POST /internal/v1/notifications/deliver`, bearer `REPLICANT_NOTIFY_DELIVERY_TOKEN_FILE`; backoff 5 s doubling to
10 min, `REPLICANT_NOTIFY_MAX_ATTEMPTS` 12) and the scheduler (PR-waiting reminders, job-lifetime escalation). Both
are advisory-lock singletons; `REPLICANT_NOTIFY_LOOPS=false` turns them off.

| Event | Recipients | Channels (default) |
|---|---|---|
| `gate.requested` (gate opened) | eligible approvers (≤ 5, suggested first, not the requester) | Slack DM, UI |
| `job.gate_waiting` | requester | issue comment, UI |
| `gate.reminder` (workflow timers) | eligible approvers | one consolidated Slack DM per person per slot, UI |
| `gate.escalated` | tenant admins (+ approvers at level 1) | Slack DM, UI |
| `gate.decided` | requester | issue comment, UI |
| `job.pr_opened` | requester | issue comment, UI, Slack DM |
| `job.pr_reminder` (scheduler) | requester + reviewers (publish-gate approvers) | issue comment (one per job), consolidated Slack DM, UI |
| `job.succeeded` / `job.failed` / other terminal | requester | issue comment, UI, Slack DM (succeeded: `digest` = DM in v0) |
| `job.lifetime_escalation` (scheduler) | tenant admins | Slack DM, UI |

Knobs (env): `REPLICANT_NOTIFY_WEB_URL`, `REPLICANT_NOTIFY_SCM_WEB_URLS` (`host=browser-base`),
`REPLICANT_NOTIFY_PR_IDLE_SECONDS` (86400), `REPLICANT_NOTIFY_PR_REMIND_EVERY_SECONDS` (86400),
`REPLICANT_NOTIFY_JOB_MAX_AGE_DAYS` (60), `REPLICANT_NOTIFY_LIFETIME_LEAD_SECONDS` (604800),
`REPLICANT_NOTIFY_SWEEP_SECONDS` (30), `REPLICANT_NOTIFY_DISPATCH_SECONDS` (2), `REPLICANT_NOTIFY_QUIET_HOURS`
(`20:00-08:00,weekends`; `off`), `REPLICANT_NOTIFY_DEFAULT_TIMEZONE` (UTC), `REPLICANT_SLACK_INTEGRATION_URL`.
`REPLICANT_NOTIFY_DIGEST_TIMES` (09:30), `REPLICANT_NOTIFY_DIGEST_THRESHOLD` (5: above it reminders go only
through the daily digest), `REPLICANT_NOTIFY_DIGEST_WEEKDAYS_ONLY` (true). Test hook (dev image only):
`PUT /v1/admin/dev/notify-overrides` (in memory). A user's `quiet_hours` preference replaces the default
(`start == end` = none). Phase 2 (NOTIFY-D12..D16): channel threads (`slack_thread`), `gate.resolved` ops, the daily
digest, `GET /internal/v1/principals/{id}/waiting` for App Home, and the `gate:decide` check on adapter decisions. Unit tests: `tests/test_notify.py`.
