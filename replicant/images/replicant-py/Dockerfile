# replicant-py: every Python service, worker, tool and one-shot of the local stack in one image (SIMPLIFY-D2).
# Build context: the monorepo root.
#   podman build -f images/replicant-py/Dockerfile --target dev  -t localhost/replicant/replicant-py:dev .
#   podman build -f images/replicant-py/Dockerfile --target prod -t localhost/replicant/replicant-py:prod .
# Installed from the workspace lockfile (uv.lock, `uv export --frozen`), so every role runs the same dependency
# versions as `uv run` and the unit suites. Each compose service picks its role with `command:`:
#   control-plane-api serve (core: github-integration, Slack, projector activities in-process) |
#   github-token-broker serve | memory-service serve | slack-mock serve | replicant-worker run |
#   publisher-worker run | python /opt/replicant/init.py (init). The github-integration, slack-integration and
#   replicant-projector entrypoints stay installed (core imports their packages).
# Targets (identity.md §9.3 L-1, as the former control-plane-api image): `prod` (default, last stage) and `dev` =
# prod plus the package replicant_dev_auth (dev seed token, static dev users, /v1/admin/dev/**). The prod stage
# never sees its source. Secrets stay per container: the image holds none, each service mounts only its own.
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM docker.io/library/python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# The workspace members `uv export` must see. cli and tests/e2e are not in the image (their pyproject only); the
# exported set is the runtime packages below without dev groups, so replicant-dev-auth is only in `dev`.
COPY pyproject.toml uv.lock ./
COPY cli/pyproject.toml cli/pyproject.toml
COPY tests/e2e/pyproject.toml tests/e2e/pyproject.toml
COPY libs/python libs/python
COPY services services
COPY workers workers
COPY tools/slack-mock tools/slack-mock
# The uv cache mount keeps third-party wheels across rebuilds; only the workspace packages are rebuilt on a change.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev --no-editable --no-hashes --no-header -o /tmp/requirements.txt \
      --package control-plane-api --package github-integration --package github-token-broker \
      --package memory-service --package slack-integration --package slack-mock --package replicant-worker \
      --package publisher-worker --package replicant-projector --package replicant-db \
 && uv venv -q /opt/venv \
 && uv pip install -q --python /opt/venv/bin/python --no-deps -r /tmp/requirements.txt
# Every role's entrypoint imports (the checks the per-service images ran).
RUN export PATH=/opt/venv/bin:$PATH && for c in control-plane-api github-integration github-token-broker memory-service \
      slack-integration slack-mock replicant-db; do "$c" --help >/dev/null; done \
 && replicant-worker nodecatalog export >/dev/null && replicant-worker local-bundle spec >/dev/null \
 && python -c "import replicant_worker.activities.catalog_impl, publisher_worker.activities, replicant_projector.activities, replicant_projector.db, boto3, httpx"

FROM build AS build-dev
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -q --python /opt/venv/bin/python --no-deps ./services/control-plane-api/replicant-dev-auth

FROM docker.io/library/python:3.12-slim AS runtime
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
# REPLICANT_BUILD_ID (services) and REPLICANT_IMAGE_BUILD_ID (workers) are the same value under the names each reads.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PATH=/opt/venv/bin:$PATH \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_BUILD_ID=${REPLICANT_BUILD_ID} \
    REPLICANT_IMAGE_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="replicant-py" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
# git: publisher-worker (applies diffs, pushes branches) and init (pushes the sample repos to Gitea).
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY images/replicant-py/init.py /opt/replicant/init.py

FROM runtime AS dev
COPY --from=build-dev /opt/venv /opt/venv
RUN python -c "import replicant_dev_auth" && git --version >/dev/null
USER 65532:65532
EXPOSE 8401 8080 8410 9464
CMD ["control-plane-api", "serve"]

FROM runtime AS prod
COPY --from=build /opt/venv /opt/venv
# L-1: CI assertion that the dev authenticator is absent from prod images
RUN if python -c "import replicant_dev_auth" 2>/dev/null; then echo "replicant_dev_auth in prod image" >&2; exit 1; fi
USER 65532:65532
EXPOSE 8401 8080 8410 9464
CMD ["control-plane-api", "serve"]
