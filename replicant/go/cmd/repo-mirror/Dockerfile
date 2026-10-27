# repo-mirror (WS-02, ADR-02-006): git http-backend behind run-token auth. Build context: the monorepo root.
FROM docker.io/library/golang:1.26 AS build
WORKDIR /src
COPY go/internal/runner go/internal/runner
COPY go/cmd/repo-mirror go/cmd/repo-mirror
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    cd go/cmd/repo-mirror && CGO_ENABLED=0 GOWORK=off go build -trimpath -ldflags="-s -w" -o /out/repo-mirror .

FROM docker.io/library/debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/cache/repo-mirror
COPY --from=build /out/repo-mirror /usr/local/bin/repo-mirror
ENTRYPOINT ["/usr/local/bin/repo-mirror"]
