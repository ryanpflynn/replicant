# Environment image env-node-service (environment definition node-service, design §8c): Node 22 with corepack (npm,
# pnpm, yarn), for Node service repos. Build context: the monorepo root.
# The agent container runs replicant-entrypoint/claude from the `tools` volume at /opt/replicant (prepare copies it).
# v0 sandboxes have no npm mirror (PLATFORM-D18), so pnpm and yarn are prepared at build time and the pnpm store is
# warm with the sample repo's lockfile (dev/sample-repos/web-console). Deliberately no libvips: the readiness probe of
# web-console reports `missing_tool` (vips).
FROM docker.io/library/node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git jq zstd make ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10002 replicant-io && groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -G 10002 -M -d /home/agent -s /bin/bash agent
ENV COREPACK_HOME=/opt/corepack COREPACK_ENABLE_DOWNLOAD_PROMPT=0 npm_config_store_dir=/opt/pnpm-store \
    npm_config_update_notifier=false
COPY dev/sample-repos/web-console/package.json dev/sample-repos/web-console/pnpm-lock.yaml /tmp/warm/
RUN corepack enable pnpm yarn \
 && corepack install --global pnpm@10.34.9 yarn@1.22.22 \
 && cd /tmp/warm && pnpm install --frozen-lockfile \
 && cd / && rm -rf /tmp/warm && chmod -R a+rwX /opt/corepack /opt/pnpm-store \
 && node --version && npm --version && pnpm --version && yarn --version
LABEL replicant.example.com/environment="node-service"
CMD ["node", "--version"]
