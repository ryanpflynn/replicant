# slack-mock

Local stand-in for the Slack Web API (parity P-14). It implements the subset slack-integration uses, records every
message in memory, and shows them at http://127.0.0.1:8410 (compose; `http://slack.replicant.localhost:8410` from
inside the stack).

```sh
uv run --package slack-mock --group dev pytest tools/slack-mock/tests -q
podman build -f tools/slack-mock/Dockerfile -t localhost/replicant/slack-mock:dev .
```

## Configuration

| Env | Meaning |
|---|---|
| `SLACK_MOCK_PORT` | listen port (8410) |
| `SLACK_MOCK_CONFIG` | workspace YAML (`dev/compose/config/slack-mock/workspace.yaml`): team, users, channels |
| `SLACK_MOCK_BOT_TOKEN_FILE` | the bot token callers must present (secret `slack_bot_token`) |
| `SLACK_MOCK_SIGNING_SECRET_FILE` | signs payloads it forwards to slack-integration (secret `slack_signing_secret`) |
| `SLACK_MOCK_INTERACTIVITY_URL`, `SLACK_MOCK_COMMANDS_URL`, `SLACK_MOCK_EVENTS_URL` | where signed payloads go |
| `SLACK_MOCK_PUBLIC_URL` | optional; base of the `response_url` in forwarded interactions (default `http://slack-mock:8410`) |

## Web API (`/api/<method>`)

`auth.test`, `chat.postMessage`, `chat.update`, `chat.postEphemeral`, `users.info`, `conversations.info`,
`conversations.open`, `views.open`. JSON, form and query arguments are merged (form fields `blocks`, `metadata`, `view`,
`attachments` may be JSON strings). Auth: `Authorization: Bearer <bot token>` (or a `token` argument):
`not_authed` / `invalid_auth`. Channels resolve by id, `#name`/`name`, `D…` or a user id (posts to `U…` land in that
user's DM). DM channel ids are deterministic: `U0ALICE01` -> `D0ALICE01`. Errors: `channel_not_found`,
`user_not_found`, `cannot_dm_bot`, `no_text`, `invalid_blocks` (not a list, or > 50 blocks), `msg_too_long`,
`message_not_found` (chat.update), `invalid_arguments` (views.open), `unknown_method` (404).

## Inspection and test API

| Route | |
|---|---|
| `GET /_mock/messages?channel=&user=&since_ts=&method=` | `{"messages": [...]}`, oldest first. Each: `id`, `method`, `channel`, `ts` (message ts), `event_ts` (when recorded; `since_ts` compares against it, so updates show up), `user` (DM recipient), `ephemeral_user`, `thread_ts`, `text`, `blocks`, `payload` (the full call arguments), `recorded_at` |
| `DELETE /_mock/messages` | clear |
| `POST /_mock/faults` `{"method": "chat.postMessage", "error": "ratelimited", "count": 1, "retry_after": 1, "status": 429}` | the next `count` calls of `method` (`*` for any) fail; `ratelimited` defaults to HTTP 429 + `Retry-After` |
| `DELETE /_mock/faults` | clear faults |
| `POST /_mock/send/interactions` `{"payload": {...}}` | sign and forward to the interactivity URL (also `commands` with `{"form": {...}}`, `events` with the raw event JSON); answers the target's status and body |
| `POST /_mock/click` (form `record_id`, `action_id`, optional `user`) | what the viewer's non-URL buttons do: a signed `block_actions` interaction as the DM recipient |
| `POST /_mock/response/{record_id}` | stand-in `response_url` (recorded as method `response_url`, or as `chat.update` with `replace_original`) |
| `GET /healthz` | |
| `GET /` | viewer: newest first, Block Kit rendered roughly, auto-refresh 5 s (`?refresh=0` to stop) |

State is in memory only; restarting the container clears it.
