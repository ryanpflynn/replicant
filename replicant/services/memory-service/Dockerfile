# memory-service (WS-04 v0 subset). Build context: the monorepo root.
#   podman build -f services/memory-service/Dockerfile -t localhost/replicant/memory-service:dev .
FROM docker.io/library/python:3.12-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="memory-service" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
COPY libs/python/replicant-ids /src/replicant-ids
COPY libs/python/replicant-telemetry /src/replicant-telemetry
COPY services/memory-service/pyproject.toml /src/memory-service/pyproject.toml
COPY services/memory-service/src /src/memory-service/src
RUN pip install --no-cache-dir /src/replicant-ids /src/replicant-telemetry /src/memory-service \
 && rm -rf /src && memory-service --help >/dev/null
USER 65532:65532
EXPOSE 8080
CMD ["memory-service", "serve"]
