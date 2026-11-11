# replicant-worker (WS-01). Build context: the monorepo root.
#   podman build -f workers/replicant-worker/Dockerfile -t localhost/replicant/replicant-worker:dev \
#     --build-arg REPLICANT_BUILD_ID=b<YYYYMMDD>-<sha12> .
# REPLICANT_BUILD_ID is the Worker Deployment build ID (C-IDS §2); compose passes the same value as
# REPLICANT_WORKER_BUILD_ID at run time (repo-layout.md §2).
FROM docker.io/library/python:3.12-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_IMAGE_BUILD_ID=${REPLICANT_BUILD_ID} REPLICANT_METRICS_BIND=0.0.0.0:9464
LABEL org.opencontainers.image.title="replicant-worker" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
COPY libs/python/replicant-temporal /src/replicant-temporal
COPY libs/python/replicant-telemetry /src/replicant-telemetry
COPY libs/python/replicant-ids /src/replicant-ids
COPY libs/python/replicant-cdef /src/replicant-cdef
COPY libs/python/replicant-scm /src/replicant-scm
COPY workers/replicant-worker/pyproject.toml /src/replicant-worker/pyproject.toml
COPY workers/replicant-worker/replicant_worker /src/replicant-worker/replicant_worker
RUN pip install --no-cache-dir /src/replicant-ids /src/replicant-cdef /src/replicant-scm /src/replicant-telemetry /src/replicant-temporal /src/replicant-worker && rm -rf /src \
 && replicant-worker nodecatalog export >/dev/null && replicant-worker local-bundle spec >/dev/null \
 && python -c "import replicant_worker.activities.catalog_impl"
USER 65532:65532
EXPOSE 9464
CMD ["replicant-worker", "run"]
