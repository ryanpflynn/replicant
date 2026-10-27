# publisher-worker

The Publisher (github-app.md §10) on task queue `replicant-publisher` (deployment `publisher-worker`, versioned):
`publisher.publish`, `publisher.push_update`, `publisher.check_base`, `publisher.close_pr`, `scm.resolve_base`,
`github.post_reply`. M1 targets local Gitea: the token comes from github-token-broker (`POST /v1/installation-tokens`,
caller token `REPLICANT_TOKEN_BROKER_TOKEN_FILE`), the approved diff bundle is checked against `approved_diff_sha256`
(G-03), forbidden and CI paths and a secret scan refuse with `PushRejected`, the commit (trailers `Replicant-Job`,
`Replicant-Activity: <idempotency key>`, `Replicant-Diff`) is pushed with git over HTTP (token via GIT_ASKPASS, never in
argv) to `replicant/<job_id>`, and the PR is found by head branch before one is created, so retries are idempotent.

```bash
podman build -f workers/publisher-worker/Dockerfile -t localhost/replicant/publisher-worker:dev .
publisher-worker run | healthcheck
uv run --package publisher-worker pytest workers/publisher-worker/tests   # starts wrk-gitea-<pid> (podman)
```
