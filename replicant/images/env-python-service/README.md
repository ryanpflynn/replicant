# env-python-service

`localhost/replicant/env-python-service:dev`: Python 3.12 (system) and 3.13 (uv-managed) with uv, plus git, jq, zstd,
make and uid 10001 `agent`, for Python service repos; the agent container runs `/opt/replicant/bin/replicant-entrypoint
run` from the `tools` volume. A warm uv cache holds the sample repo's test tooling (no PyPI mirror in v0, PLATFORM-D18).

`podman build -t localhost/replicant/env-python-service:dev -f images/env-python-service/Dockerfile .`
