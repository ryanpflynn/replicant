# dev/compose

Copy of `spec/11-local-dev/compose` (authoritative names, ports, env vars, secret paths). The only change to
`compose.yaml` is profile `v0` on the 20 M1 services. v0 overrides: `compose.v0.yaml` (temporal dev server, UI without
OIDC, gitea bootstrap v0, runner-local/gateway-shim/CONTROL tokens, SeaweedFS SSE key) and `compose.v0.deps.yaml`.
Use `replicant dev up --profile v0`; it generates `.env` and `../.secrets/`. Deviations: `docs/v0/DEVIATIONS.md` PLATFORM-D*.
Smoke: `replicant dev up --profile v0 --services postgres-replicant,migrate,temporal,temporal-bootstrap,temporal-ui,s3,s3-init,gitea,gitea-bootstrap`.
