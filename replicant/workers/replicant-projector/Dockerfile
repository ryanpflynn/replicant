# replicant-projector (WS-01 / C-STATE §2). Build context: the monorepo root.
#   podman build -f workers/replicant-projector/Dockerfile -t localhost/replicant/replicant-projector:dev .
FROM docker.io/library/python:3.12-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_IMAGE_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="replicant-projector" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
COPY libs/python/replicant-temporal /src/replicant-temporal
COPY libs/python/replicant-telemetry /src/replicant-telemetry
COPY workers/replicant-projector/pyproject.toml /src/replicant-projector/pyproject.toml
COPY workers/replicant-projector/replicant_projector /src/replicant-projector/replicant_projector
RUN pip install --no-cache-dir /src/replicant-telemetry /src/replicant-temporal /src/replicant-projector && rm -rf /src \
 && python -c "import replicant_projector.activities, replicant_projector.db"
USER 65532:65532
CMD ["replicant-projector", "run"]
