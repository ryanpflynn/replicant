-- =============================================================================
-- WS-10 · Replicant database bootstrap (run ONCE per environment, by automation)
-- =============================================================================
-- Executed by the `replicant-db-bootstrap` Kubernetes Job (config/k8s/db-bootstrap-job.yaml)
-- using the RDS-managed master secret (ManageMasterUserPassword=true). It is idempotent and is
-- the only SQL that runs with rds_superuser. No human runs it (D-26). Everything after this
-- point (schemas, tables, roles for services, grants, RLS) is created by Alembic migrations
-- running as replicant_migrator.
--
-- Locally (compose) the same file runs as the `postgres` superuser via the image's
-- /docker-entrypoint-initdb.d hook; `rds_iam` is created as a plain NOLOGIN role so grants are
-- identical (config/compose/postgres-init/00-fake-rds-roles.sql).
--
-- Validated on PostgreSQL 17 + pgvector 0.8.2 (podman, pgvector/pgvector:0.8.2-pg17-bookworm, 2026-10-24).
-- =============================================================================

\set ON_ERROR_STOP on

-- 1. The migration owner. Owns every object in database `replicant` except the audit tables
--    (owned by audit_owner, see 10-roles-and-grants.sql). LOGIN through IAM auth only in
--    AWS (no password is ever set there); locally a password from the compose .env.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicant_migrator') THEN
    CREATE ROLE replicant_migrator LOGIN CREATEROLE NOINHERIT;
  END IF;
END
$$;

-- CREATEROLE (PostgreSQL 16+ semantics): replicant_migrator can create roles and manage only the
-- roles it created (it gets ADMIN OPTION on them automatically). It cannot touch rds_superuser.
-- It needs ADMIN OPTION on rds_iam to grant IAM authentication to the service roles it creates.
GRANT rds_iam TO replicant_migrator WITH ADMIN OPTION;
-- PostgreSQL 16+: roles created by a CREATEROLE role are granted back to it WITH ADMIN OPTION
-- only. 'set' additionally lets the migrator SET ROLE to roles it created (needed for
-- ALTER DEFAULT PRIVILEGES FOR ROLE audit_owner and for audit DDL), without inheriting them.
ALTER ROLE replicant_migrator SET createrole_self_grant = 'set';

-- 2. Database ownership and lockdown.
ALTER DATABASE replicant OWNER TO replicant_migrator;
REVOKE ALL ON DATABASE replicant FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE replicant TO replicant_migrator;

-- PostgreSQL 15+ already removed CREATE on schema public from PUBLIC; make it explicit and drop
-- the public schema's usefulness entirely. Replicant uses only named schemas (C-PG).
-- Schema public holds ONLY extension objects (pgvector types/operators, pg_trgm operator classes,
-- pgcrypto functions). Nobody but its owner can create anything in it; every role may USE it.
-- (WS-04 DDL and the SQLAlchemy models resolve `vector`, `gin_trgm_ops` through search_path.)
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO replicant_migrator;
GRANT USAGE ON SCHEMA public TO PUBLIC;

-- Schema ops must exist before the first migration: Alembic creates ops.alembic_version before
-- it runs revision 202610240001_ops.
CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION replicant_migrator;
REVOKE ALL ON SCHEMA ops FROM PUBLIC;

-- 3. Extensions (C-PG §1). Created here because CREATE EXTENSION for these requires
--    rds_superuser on RDS/Aurora. They live in schema public (see above). SECURITY DEFINER
--    functions (audit.emit) pin search_path = pg_catalog, <own schema>, so they never resolve
--    names through public.
CREATE EXTENSION IF NOT EXISTS vector     WITH SCHEMA public;   -- pgvector >= 0.8 (C-PG)
CREATE EXTENSION IF NOT EXISTS pg_trgm    WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto   WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgstattuple WITH SCHEMA public;  -- bloat checks by maintenance mode
-- pg_stat_statements is enabled through the parameter group (shared_preload_libraries) and
-- created in the default database by RDS; pgaudit likewise (see config/rds/replicant-cluster-parameters.json).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public;
-- pgaudit is present on RDS/Aurora (and in Replicant's local image, config/compose/Containerfile.postgres);
-- created conditionally so the stock pgvector image still bootstraps.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgaudit') THEN
    CREATE EXTENSION IF NOT EXISTS pgaudit;
  END IF;
END
$$;

-- The migrator owns SECURITY DEFINER monitoring functions (ops.top_statements(), ops.connection_summary())
-- that expose aggregates to grafana_ro without giving Grafana pg_read_all_stats itself.
GRANT pg_read_all_stats TO replicant_migrator WITH INHERIT TRUE;

-- 4. Session defaults applied to every role in this database. Services may lower but never
--    raise these per transaction. Timeouts protect the 99.9% control-plane SLO from runaway
--    queries and from lock queues behind DDL (schema-management.md §6).
ALTER DATABASE replicant SET search_path = pg_catalog, public;
ALTER DATABASE replicant SET statement_timeout = '30s';
ALTER DATABASE replicant SET lock_timeout = '5s';
ALTER DATABASE replicant SET idle_in_transaction_session_timeout = '60s';
ALTER DATABASE replicant SET timezone = 'UTC';
-- Default for custom GUCs used by RLS (C-PG rule 4). Empty string == "no tenant" (fail closed).
ALTER DATABASE replicant SET replicant.tenant_id = '';
ALTER DATABASE replicant SET replicant.principal_scope = '';
ALTER DATABASE replicant SET replicant.audit_tenants = '';   -- limited auditors' tenant list (C-PG 2.0.0 rule 4; WS-08 audit policy)
-- PostgreSQL 15+: role-level defaults for placeholder (custom) GUCs need SET privilege on the
-- parameter. The migrator sets replicant.principal_scope='operator:all-tenants' on grafana_ro.
GRANT SET ON PARAMETER replicant.tenant_id, replicant.principal_scope TO replicant_migrator;

-- 5. Break-glass login roles (schema-management.md §10). Created here, not by migrations,
--    because their pgaudit settings are superuser-only GUCs. LOGIN only through IAM auth: the
--    IAM policy allowing rds-db:connect for these users is attached only to the approved,
--    time-boxed break-glass IAM role. Capability grants are maintained by migrations
--    (10-roles-and-grants.sql). Connection limits keep a stuck session from hogging slots.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bg_readonly') THEN
    CREATE ROLE bg_readonly LOGIN NOINHERIT CONNECTION LIMIT 5;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bg_write') THEN
    CREATE ROLE bg_write LOGIN NOINHERIT CONNECTION LIMIT 2;
  END IF;
END
$$;
GRANT rds_iam TO bg_readonly, bg_write;
GRANT CONNECT ON DATABASE replicant TO bg_readonly, bg_write;
-- Let migrations grant capability roles to the break-glass users.
GRANT bg_readonly, bg_write TO replicant_migrator WITH ADMIN OPTION, INHERIT FALSE, SET FALSE;
ALTER ROLE bg_readonly SET default_transaction_read_only = on;
ALTER ROLE bg_readonly SET statement_timeout = '60s';
ALTER ROLE bg_write SET statement_timeout = '60s';
ALTER ROLE bg_readonly SET idle_session_timeout = '15min';
ALTER ROLE bg_write SET idle_session_timeout = '15min';
-- Break-glass session audit (XD-W2-09-01; WS-09 09-security/policies/postgres/roles-and-rls.sql). These need the
-- master user (replicant_migrator gets "permission denied to set parameter"), so they live in this master-user
-- step. Unconditional: without the pgaudit library loaded they are stored as placeholders and take effect once
-- shared_preload_libraries (config/rds/replicant-cluster-parameters.json) loads it.
ALTER ROLE bg_readonly SET pgaudit.log = 'all';
ALTER ROLE bg_readonly SET pgaudit.log_parameter = on;
ALTER ROLE bg_readonly SET pgaudit.log_relation = on;
ALTER ROLE bg_write SET pgaudit.log = 'all';
ALTER ROLE bg_write SET pgaudit.log_parameter = on;
ALTER ROLE bg_write SET pgaudit.log_relation = on;
ALTER ROLE replicant_migrator SET pgaudit.log = 'ddl, role';
