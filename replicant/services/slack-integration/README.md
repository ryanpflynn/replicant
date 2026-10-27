# slack-integration

v0 scope: delivers Replicant notifications as Slack DMs for the control-plane-api dispatcher (channel `slack_dm`) and
verifies inbound Slack requests. Locally it talks to [slack-mock](../../tools/slack-mock) (`SLACK_API_URL`).

```sh
uv run --package slack-integration --group dev pytest services/slack-integration/tests -q   # no DB needed
podman build -f services/slack-integration/Dockerfile -t localhost/replicant/slack-integration:dev .
slack-integration serve | healthcheck
```

The unit suite fakes the database (`FakeRepo`) and Slack (`FakeSlack`); `tests/test_with_slack_mock.py` runs the real
slack_sdk client against slack-mock in-process.

## Routes

| Route | |
|---|---|
| `GET /healthz` | process up |
| `GET /readyz` | 200 when Postgres answers `SELECT 1` (2.5 s budget), else 503. Body also reports the cached Slack `auth.test` status (`ok`, `unreachable`, `invalid_auth`, `unknown`) and `team_id`; Slack does **not** gate readiness, so a down slack-mock or Slack outage never takes the service out of rotation (deliveries fail retryably instead). auth.test is retried in the background (2 s doubling to 60 s, then every 5 min once ok). |
| `POST /internal/v1/notifications/deliver` | NOTIFY contract (below) |
| `POST /slack/events` | signed; `url_verification` answered with the challenge, everything else acked 200 |
| `POST /slack/interactions`, `POST /slack/commands` | signed; acked 200 with `{"response_type": "ephemeral", "text": "Decisions from Slack are not available locally yet. Open the approval in Replicant to decide."}` |

Slack signature: `X-Slack-Signature` = `v0=` + hex HMAC-SHA256(signing secret, `v0:{X-Slack-Request-Timestamp}:{raw
body}`), timestamp within 300 s, constant-time compare against the current and (optional) previous secret; 401
otherwise (also when no secret is configured).

### Deliver

`Authorization: Bearer <REPLICANT_NOTIFY_CALLER_TOKEN_FILE contents>`. Flow: `slack_app.notification_posts` lookup by
`delivery_id` (hit -> `200 {"state": "sent", ..., "duplicate": true}`) -> active `identity.links` row of kind `slack`
(the bot's team from `auth.test` preferred, any namespace otherwise; none -> `200 {"state": "suppressed", "reason":
"no_slack_link"}`) -> `conversations.open` -> `chat.postMessage` (header, body section, fields section, context, URL
buttons `open_<n>`; `unfurl_*: false`; `metadata.event_type = replicant_notification`) -> insert into
`slack_app.notification_posts` -> `200 {"state": "sent", "external_ref": "<channel>:<ts>", "slack_user_id": "U…"}`.

Block Kit limits are enforced by truncation (header 150, section 3,000, 10 fields of 2,000, 5 buttons, button text
75, one `primary`), never by rejection; truncation does not cut a `<url|label>` or an `&amp;` entity. Title and field
labels are plain text and are mrkdwn-escaped (`&`, `<`, `>`) where they land in mrkdwn; `text`, field values and
`context` are passed through as mrkdwn. Links that are not http(s) are dropped.

| Outcome | HTTP | Body |
|---|---|---|
| sent / duplicate / suppressed | 200 | see above |
| malformed body (bad JSON, missing field, bad `delivery_id`) | 422 | `{"state": "rejected", "reason": "invalid_request", "errors": [...]}` |
| Slack rejects permanently (`user_not_found`, `channel_not_found`, `cannot_dm_bot`, `user_disabled`, `invalid_blocks`, `msg_too_long`, ...) | 422 | `{"state": "failed", "reason": "slack_<error>", "retryable": false}` |
| Slack `ratelimited` / HTTP 429 | 429 + `Retry-After` | `{"state": "error", "reason": "slack_ratelimited", "retryable": true, "retry_after": n}` |
| Our Slack config is wrong (`invalid_auth`, `not_authed`, `token_revoked`, `missing_scope`, no bot token file, ...) | 503 | `{"state": "error", "reason": "slack_<error>", "retryable": true}` |
| Slack unreachable / timeout / 5xx / other Slack error | 502 | `{"state": "error", "reason": "slack_<error>", "retryable": true}` |
| Database unavailable (before posting) | 503 | `{"state": "error", "reason": "db_unavailable", "retryable": true}` |
| Caller token file missing | 503 | `{"state": "error", "reason": "caller_token_not_configured"}` |
| Wrong/missing bearer | 401 | `{"state": "error", "reason": "unauthenticated"}` |

If the DM was posted but recording it fails, the answer is still `sent` with `"recorded": false` (an error is
logged): answering an error would make the dispatcher retry and post twice. Concurrent deliveries of one
`delivery_id` are serialised in-process (a single replica in v0).

Logs are JSON lines (`deliver_sent`, `deliver_failed`, `deliver_suppressed`, `slack_signature_rejected`, ...) with ids
only: no tokens, secrets or message text. `slack_sdk` logging is held at WARNING.

## Environment

| Env | Local value |
|---|---|
| `REPLICANT_ENV` | `local` |
| `REPLICANT_HTTP_PORT` | `8080` |
| `REPLICANT_DB_DSN` | `postgresql+psycopg://svc_slack_integration@postgres-replicant:5432/replicant` |
| `REPLICANT_DB_PASSWORD_FILE` | `/run/secrets/db_svc_slack_integration` |
| `SLACK_SIGNING_SECRET_FILE` | `/run/secrets/slack_signing_secret` |
| `SLACK_SIGNING_SECRET_PREVIOUS_FILE` | optional, during signing-secret rotation |
| `SLACK_BOT_TOKEN_FILE` | `/run/secrets/slack_bot_token` |
| `SLACK_API_URL` | `http://slack-mock:8410/api/` (unset: `https://slack.com/api/`) |
| `SLACK_API_TIMEOUT_SECONDS` | optional, default 10 |
| `REPLICANT_NOTIFY_CALLER_TOKEN_FILE` | `/run/secrets/notify_delivery_token` |
| `REPLICANT_LOG_LEVEL` | optional, default `INFO` |

Secret files are re-read per request (bot token: client rebuilt when it changes), so rotation needs no restart.

DB: reads `identity.links`; reads and inserts `slack_app.notification_posts` (migration `202610250020_notify_outbox`).

## Real Slack

To point it at a real workspace instead of slack-mock:

1. Create the app from [`spec/07-control-plane-and-ux/slack-app/manifest.yaml`](../../../spec/07-control-plane-and-ux/slack-app/manifest.yaml)
   (api.slack.com/apps -> Create New App -> From a manifest), replacing the `slack.replicant.acme.example.com` request
   URLs with this service's public URL, or setting `socket_mode_enabled: true` and removing them. Note: this v0
   service only has the HTTP routes; Socket Mode is not implemented, so a local install needs a tunnel to
   `/slack/events`, `/slack/interactions`, `/slack/commands` for inbound traffic. Outbound DMs need no inbound URL.
2. Install it to the workspace (bot scopes `chat:write`, `im:write`, `commands`, `users:read`, `users:read.email`).
3. Put the bot token (`xoxb-…`, OAuth & Permissions) into the `slack_bot_token` secret and the app's signing secret
   (Basic Information) into `slack_signing_secret`.
4. Unset `SLACK_API_URL` (defaults to `https://slack.com/api/`). Stop using slack-mock.
5. Link users: each Replicant principal needs an active `identity.links` row (`kind = 'slack'`, `namespace` = the
   workspace team id from `auth.test`, `external_id` = the Slack user id `U…`). In production this is Sign in with
   Slack via identity-service; locally the dev seed writes these rows (for the mock's `U0…` ids, so re-seed or insert
   rows with the real ids). Unlinked principals are `suppressed` (`no_slack_link`).

## Phase 2 (NOTIFY-D12..D15)

- **Gate decisions:** Approve (confirm dialog) / Reject (modal with required comment) on gate DMs and channel gate
  messages -> `POST /internal/v1/gates/{id}/decisions` on control-plane-api as the linked user (same authz as the UI);
  outcomes re-render the message in place; `pending_decisions` retried every `REPLICANT_SLACK_DECISION_RETRY_SECONDS`.
- **Gate resolved:** `POST /internal/v1/gates/{gate_id}/resolved` (notify bearer) re-renders every message of the gate.
- **Threads:** deliveries with `channel_id` + `thread` post a parent per job in the channel once
  (`slack_app.job_threads`) and reply under it.
- **App Home:** `app_home_opened` and every DM refresh the Home tab from `GET /internal/v1/principals/{id}/waiting`.
- Extra env: `REPLICANT_CONTROL_PLANE_URL`, `REPLICANT_CONTROL_PLANE_TOKEN_FILE` (secret `slack_integration_cp_token`),
  `REPLICANT_WEB_URL`. Real Slack additionally needs the interactivity and event request URLs (or Socket Mode, not
  implemented) and the `app_home_opened` event subscription from the manifest.

## Not implemented (v0)

- Request changes / rerun QA / answer buttons (UI only); `response_url` replies; Socket Mode; App Home refresh on
  projection changes; slash commands and shortcuts; approval-channel escalation posts; rate-limit summary DMs.
- Enterprise Grid namespaces.
