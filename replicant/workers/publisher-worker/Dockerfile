# publisher-worker (WS-07 github-app.md §10). Build context: the monorepo root.
#   podman build -f workers/publisher-worker/Dockerfile -t localhost/replicant/publisher-worker:dev .
FROM docker.io/library/python:3.12-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_IMAGE_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="publisher-worker" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY libs/python/replicant-temporal /src/replicant-temporal
COPY libs/python/replicant-telemetry /src/replicant-telemetry
COPY libs/python/replicant-scm /src/replicant-scm
COPY workers/publisher-worker/pyproject.toml /src/publisher-worker/pyproject.toml
COPY workers/publisher-worker/publisher_worker /src/publisher-worker/publisher_worker
RUN pip install --no-cache-dir /src/replicant-telemetry /src/replicant-temporal /src/replicant-scm /src/publisher-worker && rm -rf /src \
 && python -c "import publisher_worker.activities" && git --version
USER 65532:65532
CMD ["publisher-worker", "run"]
