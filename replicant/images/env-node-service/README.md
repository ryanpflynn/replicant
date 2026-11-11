# env-node-service

`localhost/replicant/env-node-service:dev`: Node 22 with corepack (npm, pnpm 10, yarn 1), plus git, jq, zstd, make and
uid 10001 `agent`, for Node service repos; the agent container runs `/opt/replicant/bin/replicant-entrypoint run` from
the `tools` volume. pnpm's store is warm with the sample repo's lockfile (no npm mirror in v0, PLATFORM-D18). The image
deliberately has no libvips, so web-console's readiness probe is `blocked` with cause `missing_tool`.

`podman build -t localhost/replicant/env-node-service:dev -f images/env-node-service/Dockerfile .`
