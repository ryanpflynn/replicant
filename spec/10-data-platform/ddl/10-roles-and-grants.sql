-- =============================================================================
-- WS-10 · Replicant database: schemas, roles, schema USAGE (C-PG 2.0.0 rules 1, 3, 4, 6)
-- =============================================================================
-- Applied by Alembic revision 202610240001_ops (migrations/versions/202610240001_ops.py) running as
-- replicant_migrator. Idempotent; re-applied by any revision that adds a schema, a login, or a reader edge.
--
-- GRANT MODEL (wave-1 fix of R1-160 / XD-ORCH-03):
--   * Table and sequence privileges in an owner's schema come ONLY from that owner's reviewed DDL
--     (C-PG rule 1: the owner DDL is the target the migrations must reproduce). The owners' narrowing
--     grants are authoritative: column-level reads (identity.sessions, identity.link_flows) and
--     append-only history (REVOKE UPDATE/DELETE) must survive. WS-10 therefore issues NO table grants
--     and NO ALTER DEFAULT PRIVILEGES ... GRANT in owner schemas.
--   * WS-10 provides: the schemas, every role and login, schema USAGE per the C-PG reader/writer
--     matrix, RLS marker roles, control-plane-api module roles, connection limits, and the grants on
--     its own schema `ops`.
--   * One documented exception: bg_readonly (break-glass) gets table-level SELECT on WS-09's six
--     operational schemas, none of which holds a narrowed (secret) column. tools/check_grants.py
--     verifies that exception and nothing else.
--   * tools/check_grants.py also checks that pg_default_acl holds nothing in owner schemas except
--     function-EXECUTE revokes, and that the owners' narrowing actually holds at runtime (R1-160 probes).
--
-- Role taxonomy
--   replicant_migrator         LOGIN (IAM), owner of every object except audit tables
--   audit_owner              NOLOGIN, owns audit.* and audit.emit() (C-PG rule 3)
--   r_ops_read               NOLOGIN, SELECT on ops (WS-10's own schema)
--   r_<schema>_read/_write   NOLOGIN grant TARGETS for owners (e.g. WS-05 grants qa tables to r_qa_read).
--                            WS-10 gives them NO table privileges; it only grants them to the logins the
--                            C-PG matrix allows (reads -> readers, writes -> the writer or its module role).
--   r_audit_emit             NOLOGIN marker; WS-08's DDL grants it EXECUTE ON audit.emit(jsonb)
--   r_audit_restore          NOLOGIN marker; WS-08 grants it EXECUTE on the restore form of audit.emit
--                            (original aud_ ID + occurred_at, only while ops.restore_events is open;
--                            CCR-10-09, XD-FIX-R1-237). Held only by replicant_migrator.
--   r_cross_tenant_read      NOLOGIN marker; RLS 'operator:all-tenants' policies target it
--   r_cross_tenant_write     NOLOGIN marker; RLS 'system:all-tenants' policies target it
--   svc_cpa_{tenancy,intake,registry,notify}  NOLOGIN module roles of control-plane-api, SET per
--                            transaction (INHERIT FALSE). Each inherits svc_cpa_read.
--   svc_cpa_read             NOLOGIN; owners grant control-plane-api's reads to it
--   memory_analytics_owner   NOLOGIN; owns WS-04's aggregate views (XD-04-12)
--   qa_analytics_owner       NOLOGIN; owns WS-05's qa.flake_summary_daily (XD-FIX-R1-113)
--   eval_metrics_reader      NOLOGIN; definer of ops.eval_* audited cross-tenant reads (CCR-06-15)
--   svc_audit_exporter       LOGIN (IAM), WS-08 audit exporter, member of audit_reader (CCR-08-06)
--   svc_<service>            LOGIN (IAM), one per deployment
--   grafana_ro               LOGIN (password; WS-09 G-18), read-only
--   bg_readonly / bg_write   LOGIN (IAM), break-glass (created by 00-bootstrap.sql)
--
-- Validated on PostgreSQL 17 + pgvector 0.8.2 (podman, pgvector/pgvector:0.8.2-pg17-bookworm, 2026-10-24).
-- =============================================================================

\set ON_ERROR_STOP on
SET createrole_self_grant = 'set';   -- also set on the role by bootstrap; explicit for clarity

-- ---------------------------------------------------------------------------
-- 1. Schemas (C-PG 1.2.0 §2: 17). All owned by replicant_migrator; `ops` and `public` come from bootstrap.
-- ---------------------------------------------------------------------------
DO $$
DECLARE s text;
BEGIN
  FOREACH s IN ARRAY ARRAY['ops','tenancy','identity','intake','registry','jobs_read','approvals',
                           'memory','ecosystem','cost','evals','audit','notify','github_app',
                           'slack_app','jira_app','qa']
  LOOP
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION replicant_migrator', s);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', s);
    -- Functions are never callable by default anywhere; owners grant EXECUTE explicitly.
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE replicant_migrator IN SCHEMA %I REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC', s);
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 2. NOLOGIN roles.
-- ---------------------------------------------------------------------------
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['audit_owner','audit_reader','r_ops_read','r_audit_emit','r_audit_restore','r_cross_tenant_read',
                           'r_cross_tenant_write','svc_cpa_tenancy','svc_cpa_intake','svc_cpa_registry',
                           'svc_cpa_notify','svc_cpa_read','memory_analytics_owner','qa_analytics_owner','eval_metrics_reader']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
  -- Capability roles: grant targets for owners' DDL only (no table privileges from WS-10).
  FOREACH r IN ARRAY ARRAY['tenancy','identity','intake','registry','jobs_read','approvals','memory',
                           'ecosystem','cost','evals','notify','github_app','slack_app','jira_app','qa']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'r_'||r||'_read') THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', 'r_'||r||'_read');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'r_'||r||'_write') THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', 'r_'||r||'_write');
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I, %I', r, 'r_'||r||'_read', 'r_'||r||'_write');
  END LOOP;
END
$$;

GRANT USAGE, CREATE ON SCHEMA audit TO audit_owner;     -- audit_owner creates audit objects (WS-08 DDL runs as it)
GRANT USAGE ON SCHEMA ops TO audit_owner;
GRANT r_cross_tenant_read, r_cross_tenant_write TO replicant_migrator WITH INHERIT TRUE;  -- data revisions, maintenance
GRANT r_audit_restore TO replicant_migrator WITH INHERIT TRUE;  -- restore_reconcile audit replay (recovery.md §4.2 step 2)

-- ops is WS-10's own schema: WS-10 is its owner, so default privileges here are owner grants.
GRANT USAGE ON SCHEMA ops TO r_ops_read;
GRANT SELECT ON ALL TABLES IN SCHEMA ops TO r_ops_read;
ALTER DEFAULT PRIVILEGES FOR ROLE replicant_migrator IN SCHEMA ops GRANT SELECT ON TABLES TO r_ops_read;

-- ---------------------------------------------------------------------------
-- 3. Logins and the C-PG matrix (schema USAGE only; table privileges come from the owners' DDL).
-- ---------------------------------------------------------------------------
-- squawk-ignore require-table-schema
CREATE TEMP TABLE _matrix (login text, writes text[], reads text[], emits_audit boolean,
                           cross_tenant_read boolean, cross_tenant_write boolean);

-- squawk-ignore require-table-schema
INSERT INTO _matrix VALUES
  -- login                    writes                       reads (USAGE)                                                audit  xt-read xt-write
  ('svc_control_plane_api',   '{tenancy,intake,registry,notify}', '{tenancy,identity,intake,registry,jobs_read,approvals,cost,evals,audit,notify,github_app,slack_app,jira_app,qa}', true, true, false),
  ('svc_identity_service',    '{identity}',                '{tenancy}',                                                 true,  true,  false),
  ('svc_replicant_projector',   '{jobs_read,approvals,qa}',  '{tenancy,intake}',                                          true,  false, false),
  ('svc_memory_service',      '{memory}',                  '{tenancy,identity,registry,jobs_read,ecosystem}',           true,  false, false),
  ('svc_ecosystem_indexer',   '{ecosystem}',               '{tenancy}',                                                 true,  false, false),
  ('svc_usage_ingestor',      '{cost}',                    '{tenancy,jobs_read}',                                       true,  false, false),
  ('svc_eval_worker',         '{evals}',                   '{tenancy,registry}',                                        true,  false, false),
  ('svc_github_integration',  '{github_app}',              '{tenancy,identity,intake,jobs_read,approvals}',             true,  false, false),
  ('svc_slack_integration',   '{slack_app}',               '{tenancy,identity,jobs_read,approvals,notify}',             true,  false, false),
  ('svc_jira_integration',    '{jira_app}',                '{tenancy,identity}',                                        true,  false, false),
  ('svc_github_token_broker', '{}',                        '{}',                                                        true,  false, false),
  ('svc_retention_sweeper',   '{}',                        '{}',                                                        true,  false, false),
  ('svc_audit_exporter',      '{}',                        '{audit}',                                                   true,  false, true),   -- system:all-tenants (ruling on XD-W2-10-03); approvals below
  ('svc_replicant_worker',      '{}',                        '{tenancy,registry,ecosystem,cost,qa}',                      false, false, false),
  ('svc_publisher_worker',    '{}',                        '{tenancy,registry}',                                        false, false, false),
  ('svc_context_mcp',         '{}',                        '{tenancy,ecosystem}',                                       false, false, false),
  -- grafana_ro: C-PG rule 6 schemas plus the schemas holding approved aggregate views (ecosystem: CCR-04-07)
  ('grafana_ro',              '{}',                        '{intake,registry,jobs_read,approvals,cost,evals,memory,audit,qa,ecosystem}', false, true, false);

DO $$
DECLARE m record; s text;
BEGIN
  FOR m IN SELECT * FROM _matrix LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = m.login) THEN
      EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT', m.login);
    END IF;
    EXECUTE format('ALTER ROLE %I NOINHERIT', m.login);
    EXECUTE format('GRANT CONNECT ON DATABASE replicant TO %I', m.login);
    EXECUTE format('GRANT rds_iam TO %I', m.login);
    EXECUTE format('GRANT r_ops_read TO %I WITH INHERIT TRUE', m.login);   -- startup compat check reads ops
    FOREACH s IN ARRAY m.reads || m.writes LOOP
      EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', s, m.login);
    END LOOP;
    -- Capability-role memberships (owners' grants to r_<schema>_* reach exactly the matrix logins).
    -- control-plane-api is handled below: its reads go to svc_cpa_read, its writes to module roles.
    -- grafana_ro gets no capability roles: it reads only what owners grant it directly (views, rule-6 tables).
    IF m.login NOT IN ('svc_control_plane_api', 'grafana_ro') THEN
      FOREACH s IN ARRAY m.reads LOOP
        IF s NOT IN ('audit') THEN
          EXECUTE format('GRANT %I TO %I WITH INHERIT TRUE', 'r_'||s||'_read', m.login);
        END IF;
      END LOOP;
      FOREACH s IN ARRAY m.writes LOOP
        EXECUTE format('GRANT %I TO %I WITH INHERIT TRUE', 'r_'||s||'_write', m.login);
      END LOOP;
    END IF;
    IF m.emits_audit THEN
      EXECUTE format('GRANT USAGE ON SCHEMA audit TO %I', m.login);
      EXECUTE format('GRANT r_audit_emit TO %I WITH INHERIT TRUE', m.login);
    END IF;
    IF m.cross_tenant_read THEN
      EXECUTE format('GRANT r_cross_tenant_read TO %I WITH INHERIT TRUE', m.login);
    END IF;
    IF m.cross_tenant_write THEN
      EXECUTE format('GRANT r_cross_tenant_write TO %I WITH INHERIT TRUE', m.login);
    END IF;
  END LOOP;
END
$$;

-- squawk-ignore ban-drop-table, require-table-schema
DROP TABLE _matrix;   -- session temp table, not a replicant table

-- control-plane-api module roles (C-PG Risks mitigation; WS-07 names). The login holds no DML; each
-- module's DML is granted by the owner's DDL to svc_cpa_<module>. Every module role inherits
-- svc_cpa_read, so a transaction running as svc_cpa_registry still has control-plane-api's reads
-- (promotion needs evals and jobs_read; R1-160).
GRANT USAGE ON SCHEMA tenancy  TO svc_cpa_tenancy;
GRANT USAGE ON SCHEMA intake   TO svc_cpa_intake;
GRANT USAGE ON SCHEMA registry TO svc_cpa_registry;
GRANT USAGE ON SCHEMA notify   TO svc_cpa_notify;
GRANT USAGE ON SCHEMA tenancy, identity, intake, registry, jobs_read, approvals, cost, evals, notify,
                github_app, slack_app, jira_app, qa TO svc_cpa_read;
GRANT svc_cpa_read TO svc_cpa_tenancy, svc_cpa_intake, svc_cpa_registry, svc_cpa_notify WITH INHERIT TRUE;
GRANT r_tenancy_read, r_identity_read, r_intake_read, r_registry_read, r_jobs_read_read, r_approvals_read,
      r_cost_read, r_evals_read, r_notify_read, r_github_app_read, r_slack_app_read, r_jira_app_read, r_qa_read
  TO svc_cpa_read WITH INHERIT TRUE;
GRANT r_tenancy_write  TO svc_cpa_tenancy  WITH INHERIT TRUE;
GRANT r_intake_write   TO svc_cpa_intake   WITH INHERIT TRUE;
GRANT r_registry_write TO svc_cpa_registry WITH INHERIT TRUE;
GRANT r_notify_write   TO svc_cpa_notify   WITH INHERIT TRUE;
GRANT r_audit_emit TO svc_cpa_tenancy, svc_cpa_intake, svc_cpa_registry, svc_cpa_notify WITH INHERIT TRUE;
GRANT USAGE ON SCHEMA audit TO svc_cpa_tenancy, svc_cpa_intake, svc_cpa_registry, svc_cpa_notify;
GRANT svc_cpa_tenancy, svc_cpa_intake, svc_cpa_registry, svc_cpa_notify TO svc_control_plane_api WITH INHERIT FALSE, SET TRUE;
GRANT svc_cpa_read TO svc_control_plane_api WITH INHERIT TRUE;

-- Owner of WS-04's aggregate-only analytics views (XD-04-12; D-25). WS-04's DDL grants it SELECT on
-- the exact base tables it aggregates.
GRANT r_cross_tenant_read TO memory_analytics_owner WITH INHERIT TRUE;
GRANT USAGE ON SCHEMA memory TO memory_analytics_owner;
GRANT r_cross_tenant_read TO qa_analytics_owner WITH INHERIT TRUE;
GRANT USAGE ON SCHEMA qa TO qa_analytics_owner;
-- The migrator inherits the view owners' privileges so an owner's DDL can still GRANT on a view after
-- handing it to the view owner (WS-05 qa-schema.sql grants grafana_ro after ALTER VIEW ... OWNER). Gives
-- the migrator nothing new: it owns every base table and already holds r_cross_tenant_read.
GRANT memory_analytics_owner, qa_analytics_owner TO replicant_migrator WITH INHERIT TRUE;
-- Same for eval_metrics_reader, so 40-eval-metrics-reader.sql stays re-runnable (CREATE OR REPLACE and GRANT on
-- functions it owns). Placed after that role's own grants below.
-- github-token-broker: column-limited reader of github_app.repos (CCR-07-11, accepted). WS-10 gives only
-- schema USAGE and no capability role; WS-07's github_app.sql grants the eight columns to the login.
GRANT USAGE ON SCHEMA github_app TO svc_github_token_broker;
-- Definer of eval-worker's audited cross-tenant read functions (CCR-06-15; ddl/40-eval-metrics-reader.sql).
-- Holds only column-level SELECT (granted in 40-*) and the system:all-tenants marker; never a login.
GRANT r_cross_tenant_write, r_audit_emit TO eval_metrics_reader WITH INHERIT TRUE;
GRANT USAGE ON SCHEMA jobs_read, intake, approvals, cost, memory, audit, ops TO eval_metrics_reader;
GRANT eval_metrics_reader TO replicant_migrator WITH INHERIT TRUE;
-- WS-08 audit exporter (CCR-08-06): reads audit.events through audit_reader.
GRANT audit_reader TO svc_audit_exporter WITH INHERIT TRUE;
-- Approval-record reconciler (XD-W2-08-03): column-limited reads granted by WS-07's approvals.sql to the login
-- (XD-W2-10-08), so WS-10 gives schema USAGE only and no r_approvals_read. The REVOKE makes re-application
-- remove the earlier capability-role membership.
GRANT USAGE ON SCHEMA approvals TO svc_audit_exporter;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member
             WHERE r.rolname = 'r_approvals_read' AND u.rolname = 'svc_audit_exporter') THEN
    REVOKE r_approvals_read FROM svc_audit_exporter;
  END IF;
END
$$;
-- Orchestrator ruling on XD-W2-10-03: audit-exporter is a service, so it uses system:all-tenants (it holds
-- r_cross_tenant_write; it has no write privilege on any table it reads).
ALTER ROLE svc_audit_exporter SET replicant.principal_scope = 'system:all-tenants';

-- grafana_ro authenticates with a PASSWORD (WS-09 G-18): Grafana's PostgreSQL data source supports only
-- user/password (or .pgpass) plus TLS [verified: https://grafana.com/docs/grafana/latest/datasources/postgres/configure/,
-- 2026-10-24]. rds_iam would forbid password login. Secrets Manager alternating-users rotation (30 d)
-- delivers it to Grafana through External Secrets. grafana_ro reads only what the owners grant it.
REVOKE rds_iam FROM grafana_ro;
ALTER ROLE grafana_ro SET replicant.principal_scope = 'operator:all-tenants';
ALTER ROLE grafana_ro SET default_transaction_read_only = on;
ALTER ROLE grafana_ro SET statement_timeout = '15s';

-- Connection budget (postgres.md §5). Sum ≈ 710 < max_connections 1000. First three: WS-09 baseline.
ALTER ROLE svc_control_plane_api   CONNECTION LIMIT 120;
ALTER ROLE svc_replicant_projector   CONNECTION LIMIT 40;
ALTER ROLE svc_memory_service      CONNECTION LIMIT 60;
ALTER ROLE svc_identity_service    CONNECTION LIMIT 40;
ALTER ROLE svc_ecosystem_indexer   CONNECTION LIMIT 20;
ALTER ROLE svc_usage_ingestor      CONNECTION LIMIT 40;
ALTER ROLE svc_eval_worker         CONNECTION LIMIT 30;
ALTER ROLE svc_github_integration  CONNECTION LIMIT 50;
ALTER ROLE svc_slack_integration   CONNECTION LIMIT 40;
ALTER ROLE svc_jira_integration    CONNECTION LIMIT 20;
ALTER ROLE svc_github_token_broker CONNECTION LIMIT 10;
ALTER ROLE svc_retention_sweeper   CONNECTION LIMIT 5;
ALTER ROLE svc_audit_exporter      CONNECTION LIMIT 4;   -- XD-FIX-R1-194
ALTER ROLE svc_replicant_worker      CONNECTION LIMIT 160;  -- up to ~6 live pinned versions × 6 pods × 4
ALTER ROLE svc_publisher_worker    CONNECTION LIMIT 20;
ALTER ROLE svc_context_mcp         CONNECTION LIMIT 40;
ALTER ROLE grafana_ro              CONNECTION LIMIT 10;

ALTER ROLE svc_control_plane_api SET statement_timeout = '15s';                     -- WS-09 baseline
ALTER ROLE svc_control_plane_api SET idle_in_transaction_session_timeout = '30s';   -- WS-09 baseline
ALTER ROLE svc_identity_service  SET statement_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 4. Break-glass (roles created by 00-bootstrap.sql).
-- ---------------------------------------------------------------------------
-- bg_readonly: WS-09's six operational schemas, table-level SELECT. This is the ONE place WS-10 grants
-- table privileges in owner schemas. None of these schemas has a narrowed secret column; append-only
-- history stays append-only (SELECT only). Re-applied for new tables by maintenance task grants_verify.
GRANT r_ops_read, r_cross_tenant_read TO bg_readonly WITH INHERIT TRUE;
GRANT USAGE ON SCHEMA jobs_read, approvals, intake, registry, cost TO bg_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA jobs_read, approvals, intake, registry, cost TO bg_readonly;
-- bg_write writes only AS a writer service: SET to that service's login, so the owner's exact
-- grants (and its audit.emit) apply. Never INHERIT; the second approver is enforced in IAM (WS-09).
GRANT bg_readonly TO bg_write WITH INHERIT TRUE;
GRANT svc_identity_service, svc_replicant_projector, svc_memory_service, svc_ecosystem_indexer,
      svc_usage_ingestor, svc_eval_worker, svc_github_integration, svc_slack_integration,
      svc_jira_integration, svc_cpa_tenancy, svc_cpa_intake, svc_cpa_registry, svc_cpa_notify
  TO bg_write WITH INHERIT FALSE, SET TRUE;
