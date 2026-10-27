# runner-local (WS-02 compose runner). Build context: the monorepo root.
FROM docker.io/library/golang:1.26 AS build
WORKDIR /src
COPY go/internal/runner go/internal/runner
COPY go/cmd/runner-local go/cmd/runner-local
ARG REPLICANT_GIT_SHA=000000000000
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    cd go/cmd/runner-local && CGO_ENABLED=0 GOWORK=off go build -trimpath -ldflags="-s -w -X main.version=0.1.0-${REPLICANT_GIT_SHA}" -o /out/runner-local .

FROM docker.io/library/alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /out/runner-local /usr/local/bin/runner-local
ENTRYPOINT ["/usr/local/bin/runner-local"]
CMD ["serve"]
