# github-integration (CONTROL, M1: Gitea adapter only)

Spec: `spec/07-control-plane-and-ux/github-app.md` §3 and §7, `triggers.md` §1 (Gitea row). One process runs the
ingress and the consumer (`--role all`, default; `ingress`/`consumer` split as in prod).

* `POST /webhooks/gitea`: `X-Gitea-Signature` (hex HMAC-SHA256 of the raw body, `gitea_webhook_secret`) is verified
  before parsing; the delivery is stored in `github_app.inbound_deliveries` keyed by `X-Gitea-Delivery`
  (`ON CONFLICT DO NOTHING`) and answered 202. Bad signature: 401.
* Consumer (SKIP LOCKED claims, exponential backoff to 10 min, 50 attempts then `dead`): normalizes with
  `replicant-scm`'s Gitea adapter and routes
  * `issues.labeled` / `issues.opened` carrying the repo's trigger label (`replicant`) from a linked user ->
    C-JOBREQ (`dedupe_key gitea:issue-label:<repo_id>:<n>:replicant`) -> `POST /v1/jobs` as `svc_github-integration`
    -> acknowledgement comment as `replicant-bot` (broker token; one per issue, `github_app.comment_refs`);
    bots are ignored, unlinked users get one reply per 24 h, refusals get one reply;
  * `pull_request.closed` on a job PR (`jobs_read.job_prs`, else branch `replicant/<job_id>`) -> `pr_event_payload` to S3
    -> Signal `pr.event` (`pull_request_merged` | `pull_request_closed`);
  * everything else is `ignored` (commands, reviews and comments are M2+).
* `POST /v1/admin/scm-installations` `{provider: gitea, host, org, tenant_id}` and `PUT /internal/v1/repo-settings`
  (behind control-plane-api's repo-settings route): register the org as installation 1, 2, … and repos with settings.
  M1 auth: the dev seed token. Repos of an installed org are also registered lazily on their first webhook.
* `GET /healthz`, `GET /readyz`, `GET /metrics` (`replicant_github_deliveries_total{outcome}`).

Identity in M1: a Gitea login in `REPLICANT_DEV_LINKED_LOGINS` (default alice,bob,carol,dave,erin,grace) is "linked" to
`derived("usr", "replicant-dev-user", login)`, the same rule control-plane-api's dev users use.

## Run and test

```sh
podman build -f services/github-integration/Dockerfile -t localhost/replicant/github-integration:dev .
CTL_GI_DB_DSN=postgresql+psycopg://svc_github_integration@127.0.0.1:55432/replicant \
CTL_GI_DB_PASSWORD_FILE=$CTL_HOME/secrets/db_svc_github_integration \
  .venv-ctl/bin/python -m pytest -q services/github-integration/tests      # needs control-plane-api tests/smoke/env.sh up
```

Env (spec compose names plus): `REPLICANT_CONTROL_PLANE_TOKEN_FILE` (bearer for `POST /v1/jobs`),
`REPLICANT_TOKEN_BROKER_TOKEN_FILE` (bearer for the broker), `REPLICANT_DEV_SEED_TOKEN_FILE` (admin routes, local only),
`REPLICANT_DEV_LINKED_LOGINS`, `REPLICANT_GITHUB_CONSUMER_WORKERS` (default 4). The component smoke is
`services/control-plane-api/tests/smoke/smoke.sh`.
