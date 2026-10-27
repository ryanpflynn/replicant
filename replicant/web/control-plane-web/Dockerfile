# control-plane-web (WS-07). Build context: the monorepo root (compose: dockerfile web/control-plane-web/Dockerfile).
#   podman build -f web/control-plane-web/Dockerfile -t localhost/replicant/control-plane-web:dev .
# Stage 1 builds the SPA with pnpm (typecheck, unit tests, vite build); stage 2 serves it with nginx on NGINX_PORT
# (8400), answers /healthz, and proxies /v1 to REPLICANT_API_UPSTREAM (spec/11-local-dev/compose/compose.yaml).
FROM docker.io/library/node:22 AS build
ARG REPLICANT_GIT_SHA=000000000000
RUN npm install -g pnpm@12.15.0 >/dev/null
WORKDIR /app
COPY web/control-plane-web/package.json web/control-plane-web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/control-plane-web/tsconfig.json web/control-plane-web/vite.config.ts web/control-plane-web/index.html ./
COPY web/control-plane-web/public ./public
COPY web/control-plane-web/src ./src
RUN pnpm test && pnpm build

FROM docker.io/library/nginx:1.29-alpine
ARG REPLICANT_BUILD_ID=b20261024-000000000000
ARG REPLICANT_GIT_SHA=000000000000
LABEL org.opencontainers.image.title="control-plane-web" org.opencontainers.image.version="${REPLICANT_BUILD_ID}" \
      org.opencontainers.image.revision="${REPLICANT_GIT_SHA}"
ENV NGINX_PORT=8400 REPLICANT_API_UPSTREAM=http://control-plane-api:8401 NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx/conf.d
RUN rm -f /etc/nginx/conf.d/default.conf
COPY web/control-plane-web/deploy/default.conf.template /etc/nginx/templates/default.conf.template
COPY --chmod=0755 web/control-plane-web/deploy/15-resolver.envsh /docker-entrypoint.d/15-resolver.envsh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8400
HEALTHCHECK --interval=10s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:${NGINX_PORT}/healthz || exit 1
