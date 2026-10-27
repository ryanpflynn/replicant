# replicant CLI image for Claude Code (WS-03 decision 2): the pinned CLI plus /opt/replicant/bin/{replicant-entrypoint,
# replicant-agentd,replicant-qa}. Build context: the monorepo root. arm64/amd64 via the npm per-platform package.
ARG CLAUDE_CODE_VERSION=2.1.310

FROM docker.io/library/golang:1.26 AS build
WORKDIR /src
COPY go/internal/runner go/internal/runner
COPY go/cmd/replicant-entrypoint go/cmd/replicant-entrypoint
COPY go/cmd/replicant-agentd go/cmd/replicant-agentd
COPY go/cmd/replicant-qa go/cmd/replicant-qa
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    for c in replicant-entrypoint replicant-agentd replicant-qa; do \
      (cd go/cmd/$c && CGO_ENABLED=0 GOWORK=off go build -trimpath -ldflags="-s -w" -o /out/$c .) || exit 1; done

FROM docker.io/library/debian:bookworm-slim AS cli
ARG CLAUDE_CODE_VERSION
# npm dist.integrity of @anthropic-ai/claude-code-linux-<arch>@<version> (verified at build).
ARG CLAUDE_CODE_SHA512_ARM64=iAukGiGmsInasuW34o4ExD8Gqh3b/CPUp6kjxsWIBT8tFBrVFxtWGfI7wfRy3owWrbuRekBCaHQFMiI2ddWUpA==
ARG CLAUDE_CODE_SHA512_AMD64=
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates openssl && rm -rf /var/lib/apt/lists/*
RUN set -eu; arch="$(dpkg --print-architecture)"; case "$arch" in arm64) pkg=linux-arm64; want="$CLAUDE_CODE_SHA512_ARM64";; \
      amd64) pkg=linux-x64; want="$CLAUDE_CODE_SHA512_AMD64";; *) echo "unsupported $arch"; exit 1;; esac; \
    curl -fsSLo /tmp/cc.tgz "https://registry.npmjs.org/@anthropic-ai/claude-code-${pkg}/-/claude-code-${pkg}-${CLAUDE_CODE_VERSION}.tgz"; \
    got="$(openssl dgst -sha512 -binary /tmp/cc.tgz | base64 -w0)"; \
    if [ -n "$want" ] && [ "$got" != "$want" ]; then echo "integrity mismatch: $got"; exit 1; fi; echo "integrity ok"; \
    mkdir -p /opt/replicant/cli/claude-code/${CLAUDE_CODE_VERSION} && tar -xzf /tmp/cc.tgz -C /tmp && \
    install -m 0755 /tmp/package/claude /opt/replicant/cli/claude-code/${CLAUDE_CODE_VERSION}/claude && rm -rf /tmp/cc.tgz /tmp/package

FROM docker.io/library/debian:bookworm-slim
ARG CLAUDE_CODE_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates bash && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10002 replicant-io && groupadd -g 10000 agentd && groupadd -g 10001 agent && groupadd -g 10003 sidecar \
 && useradd -u 10000 -g 10000 -G 10002 -M -d /nonexistent -s /usr/sbin/nologin agentd \
 && useradd -u 10001 -g 10001 -G 10002 -M -d /home/agent -s /bin/bash agent \
 && useradd -u 10003 -g 10003 -G 10002 -M -d /nonexistent -s /usr/sbin/nologin sidecar
COPY --from=cli /opt/replicant/cli /opt/replicant/cli
COPY --from=build /out/ /opt/replicant/bin/
COPY images/cli-claude-code/gitconfig /opt/replicant/etc/gitconfig
LABEL org.opencontainers.image.version="${CLAUDE_CODE_VERSION}" \
      replicant.example.com/cli="claude-code" \
      replicant.example.com/entrypoint-version="1.1.0" \
      replicant.example.com/replicant-qa-version="0.1.0"
ENV PATH=/opt/replicant/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENTRYPOINT []
CMD ["/opt/replicant/bin/replicant-entrypoint", "version"]
