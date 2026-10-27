# Replicant CLI

`replicant dev` drives the local stack with podman + podman-compose (project `replicant`). REPLICANT_ENV must be `local`.

```sh
uv run replicant dev up --profile v0          # secrets -> dev/.secrets, build replicant images, start, wait healthy
uv run replicant dev up --profile v0 --services gitea,gitea-bootstrap --no-build   # a subset (plus deps)
uv run replicant dev up --profile v0 --with-observability   # + Alloy/Loki/Tempo/Prometheus/Grafana, OTel on
uv run replicant dev up --profile v0 --gateway passthrough  # key from replicant/dev/.env ANTHROPIC_API_KEY (never printed)
uv run replicant dev status | logs [svc...] [-f] | down [--volumes]
uv run replicant dev seed [--only 02-tenants] [--force] [--dry-run]
```

Services whose Dockerfile does not exist yet (or whose dependency is skipped) are reported and skipped.
Tests: `uv run --package replicant-cli --group dev pytest cli/tests`.
