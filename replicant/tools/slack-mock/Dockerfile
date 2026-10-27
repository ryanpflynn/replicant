# slack-mock (parity P-14). Build context: the monorepo root.
#   podman build -f tools/slack-mock/Dockerfile -t localhost/replicant/slack-mock:dev .
FROM docker.io/library/python:3.12-slim
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLICANT_GIT_SHA=${REPLICANT_GIT_SHA} REPLICANT_BUILD_ID=${REPLICANT_BUILD_ID}
LABEL org.opencontainers.image.title="slack-mock" org.opencontainers.image.version="${REPLICANT_BUILD_ID}"
COPY tools/slack-mock/pyproject.toml /src/slack-mock/pyproject.toml
COPY tools/slack-mock/src /src/slack-mock/src
RUN pip install --no-cache-dir /src/slack-mock && rm -rf /src && slack-mock --help >/dev/null
USER 65532:65532
EXPOSE 8410
CMD ["slack-mock", "serve"]
