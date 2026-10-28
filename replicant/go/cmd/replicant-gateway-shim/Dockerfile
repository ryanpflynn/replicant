# replicant-gateway-shim (WS-03). Build context: the monorepo root
#   podman build -f go/cmd/replicant-gateway-shim/Dockerfile -t localhost/replicant/replicant-gateway-shim:dev .
FROM docker.io/library/golang:1.26 AS build
WORKDIR /src/go/cmd/replicant-gateway-shim
COPY go/internal/runner /src/go/internal/runner
COPY go/cmd/replicant-gateway-shim/go.mod go/cmd/replicant-gateway-shim/go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod GOWORK=off go mod download
COPY go/cmd/replicant-gateway-shim/ ./
ARG REPLICANT_VERSION=dev
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOWORK=off go build -trimpath -ldflags="-s -w" -o /out/replicant-gateway-shim . \
 && mkdir -p /out/var/lib/gateway-shim/usage

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/replicant-gateway-shim /usr/local/bin/replicant-gateway-shim
COPY --from=build --chown=65532:65532 /out/var/lib/gateway-shim /var/lib/gateway-shim
USER 65532:65532
EXPOSE 8420
ENTRYPOINT []
CMD ["/usr/local/bin/replicant-gateway-shim", "--config=/etc/gateway-shim/gateway-shim.yaml", "--upstream-backend=script"]
