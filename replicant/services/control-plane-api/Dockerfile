# control-plane-api (WS-07). Build context: the monorepo root.
#   podman build -f services/control-plane-api/Dockerfile --target dev  -t localhost/replicant/control-plane-api:dev .
#   podman build -f services/control-plane-api/Dockerfile --target prod -t localhost/replicant/control-plane-api:prod .
# Targets (architecture.md §2.7, identity.md §9.3 L-1): `prod` (default, last stage) and `dev` = prod plus the
# package replicant_dev_auth (dev seed token, static dev users, /v1/admin/dev/**). The prod stage never sees its source.
# The same image serves `control-plane-api serve` (port 8401) and `control-plane-api serve-grants --port 8403`.
FROM docker.io/library/python:3.12-slim AS base
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="control-plane-api" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
COPY libs/python/replicant-ids /src/replicant-ids
COPY libs/python/replicant-temporal /src/replicant-temporal
COPY libs/python/replicant-telemetry /src/replicant-telemetry
COPY libs/python/replicant-cdef /src/replicant-cdef
COPY services/control-plane-api/pyproject.toml /src/control-plane-api/pyproject.toml
COPY services/control-plane-api/src /src/control-plane-api/src
RUN pip install --no-cache-dir /src/replicant-ids /src/replicant-telemetry /src/replicant-temporal /src/replicant-cdef /src/control-plane-api \
 && rm -rf /src && control-plane-api --help >/dev/null

FROM scratch AS dev-auth-src
COPY services/control-plane-api/replicant-dev-auth /replicant-dev-auth

FROM base AS dev
COPY --from=dev-auth-src /replicant-dev-auth /src/replicant-dev-auth
RUN pip install --no-cache-dir /src/replicant-dev-auth && rm -rf /src && python -c "import replicant_dev_auth"
USER 65532:65532
EXPOSE 8401 8403
CMD ["control-plane-api", "serve"]

FROM base AS prod
# L-1: CI assertion that the dev authenticator is absent from prod images
RUN if python -c "import replicant_dev_auth" 2>/dev/null; then echo "replicant_dev_auth in prod image" >&2; exit 1; fi
USER 65532:65532
EXPOSE 8401 8403
CMD ["control-plane-api", "serve"]
