# sandbox-sidecars (WS-02 local image: uploader, watchdog, workspace-guard, volume-init) and, with
# --target pause, the pod-group pause image (localhost/replicant/pause:3.10). Build context: the monorepo root.
FROM docker.io/library/golang:1.26 AS build
WORKDIR /src
COPY go/internal/runner go/internal/runner
COPY go/cmd/sandbox-sidecars go/cmd/sandbox-sidecars
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    cd go/cmd/sandbox-sidecars && CGO_ENABLED=0 GOWORK=off go build -trimpath -ldflags="-s -w" -o /out/sandbox-sidecars . \
 && cp /out/sandbox-sidecars /out/pause

FROM scratch AS pause
COPY --from=build /out/pause /pause
USER 65535:65535
ENTRYPOINT ["/pause"]

FROM docker.io/library/alpine:3.20 AS sidecars
RUN apk add --no-cache ca-certificates
COPY --from=build /out/sandbox-sidecars /usr/local/bin/sandbox-sidecars
ENTRYPOINT []
CMD ["/usr/local/bin/sandbox-sidecars", "version"]
