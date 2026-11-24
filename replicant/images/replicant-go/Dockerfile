# replicant-go: the Go services of the local stack in one image (SIMPLIFY-D2): runner-local and replicant-gateway-shim.
# Build context: the monorepo root.
#   podman build -f images/replicant-go/Dockerfile -t localhost/replicant/replicant-go:dev .
# Each compose service picks its binary with `command:` (no ENTRYPOINT). Each module builds on its own (GOWORK=off,
# its go.mod replaces example.com/replicant/internal/runner with ../../internal/runner), as the per-binary images did.
# debian-slim with CA certificates: runner-local talks to S3/STS and the engine socket. The sandbox images (sandbox-sidecars, pause) are separate.
FROM docker.io/library/golang:1.26 AS build
WORKDIR /src
COPY go/internal/runner go/internal/runner
COPY go/cmd/runner-local go/cmd/runner-local
COPY go/cmd/replicant-gateway-shim go/cmd/replicant-gateway-shim
ARG REPLICANT_GIT_SHA=000000000000
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    export CGO_ENABLED=0 GOWORK=off \
 && (cd go/cmd/runner-local && go build -trimpath -ldflags="-s -w -X main.version=0.1.0-${REPLICANT_GIT_SHA}" -o /out/runner-local .) \
 && (cd go/cmd/replicant-gateway-shim && go build -trimpath -ldflags="-s -w" -o /out/replicant-gateway-shim .)

FROM docker.io/library/debian:bookworm-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
LABEL org.opencontainers.image.title="replicant-go" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/lib/gateway-shim/usage && chown -R 65532:65532 /var/lib/gateway-shim
COPY --from=build /out/ /usr/local/bin/
# runner-local runs as root (the engine socket); compose runs gateway-shim as
# 65532:65532, its user in the former distroless image.
EXPOSE 8480 8490 8420
ENTRYPOINT []
CMD ["/usr/local/bin/runner-local", "serve"]
