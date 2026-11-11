# Environment image env-python-service (environment definition python-service, design §8c): Python 3.12 (system) and
# 3.13 (uv-managed) with uv, for Python service repos. Build context: the monorepo root.
# The agent container runs replicant-entrypoint/claude from the `tools` volume at /opt/replicant (prepare copies it).
# v0 sandboxes have no PyPI mirror (PLATFORM-D18), so the image carries a warm uv cache with the test tooling the
# sample repo pins (dev/sample-repos/billing-py/uv.lock); other packages fail at install as cause `network`.
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM docker.io/library/python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends git jq zstd make ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10002 replicant-io && groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -G 10002 -M -d /home/agent -s /bin/bash agent
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_CACHE_DIR=/opt/uv/cache UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1
COPY dev/sample-repos/billing-py/pyproject.toml dev/sample-repos/billing-py/uv.lock /tmp/warm/
RUN UV_PYTHON_DOWNLOADS=automatic uv python install 3.13 \
 && cd /tmp/warm && uv sync --frozen --python 3.12 && uv sync --frozen --python 3.13 \
 && cd / && rm -rf /tmp/warm && chmod -R a+rwX /opt/uv \
 && python3.12 --version && uv run --no-project --python 3.13 python --version
LABEL replicant.example.com/environment="python-service"
CMD ["python3", "--version"]
