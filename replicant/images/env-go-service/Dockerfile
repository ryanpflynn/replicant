# Environment image env-go-service (WS-03 environment-definition go-service): Go toolchain for Go service repos.
# The agent container runs replicant-entrypoint/claude from the `tools` volume at /opt/replicant (prepare copies it).
FROM docker.io/library/golang:1.26
RUN apt-get update && apt-get install -y --no-install-recommends jq zstd make && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10002 replicant-io && groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -G 10002 -M -d /home/agent -s /bin/bash agent \
 && chmod -R a+rwX /go
ENV GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0
LABEL replicant.example.com/environment="go-service"
CMD ["go", "version"]
