# sandbox-sidecars (WS-02 local image: uploader with the disk watchdog, sandbox-init, and `pause`, the pod-group
# network namespace holder runner-local starts from this same image). Build context: the monorepo root.
FROM docker.io/library/golang:1.26 AS build
WORKDIR /src
COPY go/internal/runner go/internal/runner
COPY go/cmd/sandbox-sidecars go/cmd/sandbox-sidecars
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    cd go/cmd/sandbox-sidecars && CGO_ENABLED=0 GOWORK=off go build -trimpath -ldflags="-s -w" -o /out/sandbox-sidecars .

FROM docker.io/library/alpine:3.20 AS sidecars
RUN apk add --no-cache ca-certificates
COPY --from=build /out/sandbox-sidecars /usr/local/bin/sandbox-sidecars
ENTRYPOINT []
CMD ["/usr/local/bin/sandbox-sidecars", "version"]
