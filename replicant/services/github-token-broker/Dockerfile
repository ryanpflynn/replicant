# github-token-broker (WS-07, ADR-011). Build context: the monorepo root.
#   podman build -f services/github-token-broker/Dockerfile -t localhost/replicant/github-token-broker:dev .
FROM docker.io/library/python:3.12-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="github-token-broker" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
COPY libs/python/replicant-ids /src/replicant-ids
COPY libs/python/replicant-scm /src/replicant-scm
COPY libs/python/replicant-telemetry /src/replicant-telemetry
COPY services/github-token-broker/pyproject.toml /src/github-token-broker/pyproject.toml
COPY services/github-token-broker/src /src/github-token-broker/src
RUN pip install --no-cache-dir /src/replicant-ids /src/replicant-scm /src/replicant-telemetry /src/github-token-broker \
 && rm -rf /src && github-token-broker --help >/dev/null
USER 65532:65532
EXPOSE 8080
CMD ["github-token-broker", "serve"]
