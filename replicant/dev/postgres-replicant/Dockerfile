# Local replicant Postgres (stack.md §2): prod parity with Aurora PG 17 + pgvector 0.8.2 + pgaudit (XD-10-03).
#   podman build -t localhost/replicant/postgres:17-pgvector0.8.2 dev/postgres-replicant
FROM docker.io/pgvector/pgvector:0.8.2-pg17-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-pgaudit \
 && rm -rf /var/lib/apt/lists/*
