# replicant-db

The Replicant database package (WS-10 framework from `spec/10-data-platform/code/replicant_db`): one linear Alembic
history in `replicant_db/migrations/versions/` = `202610240001_ops` (roles, grants, ops) + **all 17 owner DDL files**
(INDEX.md §2 order, applied verbatim from `replicant_db/sql/owners/`) + `202610240019_eval_reader` + `202610250020_notify_outbox` (v0 notification outbox, DEVIATIONS NOTIFY-D1). Head:
`202610250020_notify_outbox` (`replicant_db._build.BUILT_REVISION`, the C-PG rule-8 startup check value).
`bootstrap/` holds the superuser init SQL that postgres-replicant runs once.

- CLI: `replicant-db migrate --to head --advisory-lock --verify-drift --ensure-roles`, `replicant-db migrate status`,
  `replicant-db maintenance run`. Locally migrate also creates/secures partitions (DEVIATIONS PLATFORM-D5).
- Image: `podman build -f libs/python/replicant-db/Dockerfile.migrate -t localhost/replicant/migrate:dev .` (repo root).
- Unit tests: `uv run --package replicant-db --group dev pytest libs/python/replicant-db`
- Migration gate (podman, ~2 min): `STRICT=1 libs/python/replicant-db/tools/validate_migrations.sh` — fresh migrate,
  no-op rerun, downgrade/upgrade/check round trip, lint, catalog diff against the spec DDL built with psql (all 17
  schemas), partition isolation, eval-reader and audit-exporter probes, grants and RLS checks.
