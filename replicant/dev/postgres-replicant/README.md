# postgres-replicant image

`FROM pgvector/pgvector:0.8.2-pg17-bookworm` plus `postgresql-17-pgaudit`. The compose service mounts WS-10's
`00-fake-rds-roles.sql` and `00-bootstrap.sql` (from `libs/python/replicant-db/bootstrap/`) and
`dev/compose/config/postgres-replicant/02-migrator-password.sh` as init scripts; everything else comes from `migrate`.

Build: `podman build -t localhost/replicant/postgres:17-pgvector0.8.2 dev/postgres-replicant`
Smoke: `replicant dev up --profile v0` then `replicant dev status` (postgres-replicant healthy, migrate exited 0).
