# github-integration (WS-07). Build context: the monorepo root.
#   podman build -f services/github-integration/Dockerfile -t localhost/replicant/github-integration:dev .
FROM docker.io/library/python:3.12-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="github-integration" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
COPY libs/python/replicant-ids /src/replicant-ids
COPY libs/python/replicant-scm /src/replicant-scm
COPY libs/python/replicant-temporal /src/replicant-temporal
COPY libs/python/replicant-telemetry /src/replicant-telemetry
COPY services/github-integration/pyproject.toml /src/github-integration/pyproject.toml
COPY services/github-integration/src /src/github-integration/src
RUN pip install --no-cache-dir /src/replicant-ids /src/replicant-scm /src/replicant-telemetry /src/replicant-temporal /src/github-integration \
 && rm -rf /src && github-integration --help >/dev/null
USER 65532:65532
EXPOSE 8080
CMD ["github-integration", "serve"]
