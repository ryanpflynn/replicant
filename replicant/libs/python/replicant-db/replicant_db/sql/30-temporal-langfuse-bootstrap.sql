-- =============================================================================
-- WS-10 · Bootstrap of the `temporal` and `langfuse` instances (run once per environment by the
-- db-bootstrap Job with the RDS-managed master secret; idempotent). Schema contents are owned by
-- Temporal (temporal-sql-tool) and Langfuse (Prisma), never hand-written (D-26).
-- Validated on PostgreSQL 16.x and 17.11 (podman, 2026-10-24).
-- =============================================================================

-- ---- Instance `temporal` (Aurora PostgreSQL 16; C-PG §1) -----------------------------------
-- temporal_admin: owns the databases and every table; used only by the temporal-schema Job.
-- temporal_server: DML only; used by Temporal frontend/history/matching/worker pods.
-- Both IAM-authenticated (rds_iam): no passwords exist.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'temporal_admin') THEN
    CREATE ROLE temporal_admin LOGIN CREATEDB CONNECTION LIMIT 5;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'temporal_server') THEN
    CREATE ROLE temporal_server LOGIN CONNECTION LIMIT 600;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'temporal_grafana_ro') THEN
    CREATE ROLE temporal_grafana_ro LOGIN CONNECTION LIMIT 5;
  END IF;
END
$$;
GRANT rds_iam TO temporal_admin, temporal_server, temporal_grafana_ro;
-- Default privileges are per database; the db-bootstrap Job runs the block below in BOTH
-- `temporal` and `temporal_visibility` after create-database (it connects to each in turn).
-- \connect temporal / \connect temporal_visibility
ALTER DEFAULT PRIVILEGES FOR ROLE temporal_admin IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO temporal_server;
ALTER DEFAULT PRIVILEGES FOR ROLE temporal_admin IN SCHEMA public
  GRANT SELECT ON TABLES TO temporal_grafana_ro;
ALTER ROLE temporal_server SET statement_timeout = '30s';
ALTER ROLE temporal_grafana_ro SET default_transaction_read_only = on;
ALTER ROLE temporal_grafana_ro SET statement_timeout = '15s';

-- ---- Instance `langfuse` (Aurora PostgreSQL 16; Langfuse v4 needs PostgreSQL >= 15; R1-235) -----
-- Databases: `langfuse` (Langfuse) and `grafana` (Grafana's own state, owned by grafana_app; WS-08).
-- Langfuse applies Prisma migrations at web-container start using DIRECT_URL ("used for database
-- migrations. Use this if you want to use a different user for migrations")
-- [verified: https://langfuse.com/self-hosting/configuration, 2026-10-24].
-- langfuse_migrator: DIRECT_URL (owner). langfuse_app: DATABASE_URL (DML only).
-- Langfuse connects with static connection strings, so these two roles use passwords managed by
-- Secrets Manager rotation (no rds_iam: a role with rds_iam can no longer log in with a password).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'langfuse_migrator') THEN
    CREATE ROLE langfuse_migrator LOGIN CONNECTION LIMIT 5;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'langfuse_app') THEN
    CREATE ROLE langfuse_app LOGIN CONNECTION LIMIT 200;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_app') THEN
    CREATE ROLE grafana_app LOGIN CONNECTION LIMIT 20;   -- Grafana's own database `grafana` (R1-235)
  END IF;
END
$$;
-- \connect langfuse
-- ALTER DATABASE langfuse OWNER TO langfuse_migrator;   (run against the langfuse instance)
-- CREATE DATABASE grafana OWNER grafana_app;            (same instance; Grafana migrates its own schema)
-- ALTER DEFAULT PRIVILEGES FOR ROLE langfuse_migrator IN SCHEMA public
--   GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO langfuse_app;
-- ALTER DEFAULT PRIVILEGES FOR ROLE langfuse_migrator IN SCHEMA public
--   GRANT USAGE, SELECT ON SEQUENCES TO langfuse_app;
