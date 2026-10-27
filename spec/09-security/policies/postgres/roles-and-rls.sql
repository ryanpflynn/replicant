-- WS-09 security requirements for database `replicant` (C-PG 2.0.0 rules 1, 3, 4, 6).
-- AUTHORITY (R1-228, XD-10-06, XD-FIX-R1-160 grant model v2): WS-10's `10-data-platform/ddl/00-bootstrap.sql`
-- and `10-roles-and-grants.sql` are the single definition of every login role, connection limit, marker
-- role and schema USAGE; each schema owner grants table privileges in its own DDL. WS-09 creates no role,
-- grants no table privilege and defines no policy on another owner's table. WS-09 accepts WS-10's
-- role-gated cross-tenant reads (policies `TO r_cross_tenant_read`): no BYPASSRLS login exists, and the
-- GUC replicant.principal_scope alone can never widen visibility.
--
-- Requirements this file states for the owners and WS-10's validator (validate.sh, check_grants.py,
-- check_rls.py, check_partitions.py):
--   R1. Every service login is NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS and a member
--       of rds_iam (IAM authentication only; IAM takes precedence over passwords for rds_iam members
--       [verified: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html, 2026-10-24]).
--   R2. Break-glass roles bg_readonly and bg_write exist only as WS-10 defines them; IAM resource
--       dbuser:cluster-*/<role> (policies/iam/db-breakglass.role-permissions.json).
--   R3. Every table with tenant_id has ENABLE and FORCE ROW LEVEL SECURITY, fails closed when
--       replicant.tenant_id is unset, and every partition of a partitioned tenant table is isolated
--       (XD-ORCH-04: no direct partition grants; RLS applied to each partition).
--   R4. Session variables are set per transaction with set_config(..., true); the canonical
--       replicant.principal_scope values are WS-10's enum.
--   R5. svc_github_token_broker holds only SELECT (provider, host, repo_id, installation_id, tenant_id,
--       archived, removed_at, indexing_enabled) on github_app.repos (CCR-07-11; WS-07 grants it).
--
-- Application path (validated 2026-10-24 on WS-10's framework): these ALTER ROLE ... SET pgaudit.* statements
-- need the RDS master user (rds_superuser); as replicant_migrator they fail with "permission denied to set
-- parameter". WS-10 therefore runs them in its bootstrap step (00-bootstrap.sql), not in Alembic (XD-W2-09-01).
--
-- The only statements WS-09 adds: pgaudit session audit for the break-glass roles (D-26). CloudTrail
-- does not log IAM DB authentication [verified: same page], so pgaudit plus the SSM session log is the
-- audit trail. pgaudit must be in shared_preload_libraries (parameter group, WS-10); on a server without
-- the pgaudit library loaded these settings are accepted as placeholders and take effect once loaded.
ALTER ROLE bg_readonly SET pgaudit.log = 'all';
ALTER ROLE bg_readonly SET pgaudit.log_parameter = on;
ALTER ROLE bg_readonly SET pgaudit.log_relation = on;
ALTER ROLE bg_write SET pgaudit.log = 'all';
ALTER ROLE bg_write SET pgaudit.log_parameter = on;
ALTER ROLE bg_write SET pgaudit.log_relation = on;
