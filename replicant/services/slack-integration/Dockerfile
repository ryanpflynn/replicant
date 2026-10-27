# slack-integration. Build context: the monorepo root.
#   podman build -f services/slack-integration/Dockerfile -t localhost/replicant/slack-integration:dev .
FROM docker.io/library/python:3.12-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="slack-integration" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
COPY libs/python/replicant-ids /src/replicant-ids
COPY libs/python/replicant-telemetry /src/replicant-telemetry
COPY services/slack-integration/pyproject.toml /src/slack-integration/pyproject.toml
COPY services/slack-integration/src /src/slack-integration/src
RUN pip install --no-cache-dir /src/replicant-ids /src/replicant-telemetry /src/slack-integration && rm -rf /src && slack-integration --help >/dev/null
USER 65532:65532
EXPOSE 8080
CMD ["slack-integration", "serve"]
