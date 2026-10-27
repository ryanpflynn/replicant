# github-token-broker (CONTROL, M1: Gitea backend)

Spec: `spec/07-control-plane-and-ux/github-app.md` §9, `api/github-token-broker.openapi.yaml`, ADR-011 (as amended
by CCR-09-06), parity P-12. The broker is the only holder of the `replicant-bot` credential
(`REPLICANT_BROKER_GITEA_BOT_PASSWORD_FILE`).

* `POST /v1/installation-tokens` (`TokenRequest` -> `TokenResponse`): the caller (static bearer token from
  `REPLICANT_BROKER_CALLER_TOKENS_FILE`, local only) must be in `broker-callers.yaml` and may request only a subset of its
  `max_permissions` and at most `repos_per_request` repos; `workflows`, `administration`, `secrets`, `actions` are always
  refused (403 `permission_not_allowed`). The broker mints a Gitea access token for `replicant-bot` with the mapped
  scopes (`providers.gitea.scope_map`), caches it per (caller, installation, repos, permissions) until 10 min before
  expiry, and deletes it after `REPLICANT_BROKER_GITEA_TOKEN_TTL` (reaper every 60 s; Gitea tokens never expire).
  Every mint and refusal emits `github.token_minted` (`audit.emit` via `svc_github_token_broker`). Token values are never
  logged.
* GitHub App operations (`/v1/app/...`) answer 501 until the github backend (M3).
* `GET /healthz`, `GET /readyz` (bot credential present, Gitea `/api/healthz` reachable, callers configured).

## Run and test

```sh
podman build -f services/github-token-broker/Dockerfile -t localhost/replicant/github-token-broker:dev .
.venv-ctl/bin/python -m pytest -q services/github-token-broker/tests     # Gitea is an httpx MockTransport fake
```
