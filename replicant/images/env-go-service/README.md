# env-go-service

`localhost/replicant/env-go-service:dev`: Go 1.26 toolchain (golang:1.26 + jq, zstd, make, uid 10001 `agent`) for Go
service repos; the agent container runs `/opt/replicant/bin/replicant-entrypoint run` from the `tools` volume.

`podman build -t localhost/replicant/env-go-service:dev -f images/env-go-service/Dockerfile .`
