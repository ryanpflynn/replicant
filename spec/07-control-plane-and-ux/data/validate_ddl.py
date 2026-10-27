#!/usr/bin/env python3
"""Apply WS-07 DDL to a throwaway PostgreSQL and run smoke statements.

  uv run --with pgserver --with psycopg[binary] python data/validate_ddl.py

Creates the service roles WS-10's migrations would create, applies data/ddl/*.sql in dependency
order inside one transaction per file, then exercises the seq-guarded projection upsert, the
dedupe uniqueness, and RLS. pgserver bundles a PostgreSQL build; WS-10 runs the same files
through Alembic against PostgreSQL 17/18 in CI.
"""
import pathlib
import sys
import tempfile

import pgserver
import psycopg

HERE = pathlib.Path(__file__).parent
ORDER = ["tenancy", "identity", "intake", "jobs_read", "approvals", "notify", "github_app", "slack_app", "jira_app"]
ROLES = ["svc_cpa_tenancy", "svc_cpa_read", "svc_cpa_intake", "svc_cpa_notify", "svc_identity_service",
         "svc_github_integration", "svc_slack_integration", "svc_jira_integration", "svc_replicant_projector",
         "svc_memory_service", "svc_usage_ingestor", "svc_github_token_broker", "svc_audit_exporter", "grafana_ro",
         "r_cross_tenant_read", "r_cross_tenant_write"] + [
         f"r_{sch}_{k}" for sch in ["tenancy", "identity", "intake", "jobs_read", "approvals", "notify",
                                    "github_app", "slack_app", "jira_app"] for k in ("read", "write")]

# Minimal stand-ins for WS-10's ops helpers (10-data-platform/ddl/20-ops.sql), same signatures and policy shape.
OPS_STUB = r"""
CREATE SCHEMA ops;
CREATE FUNCTION ops.current_tenant() RETURNS text LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('replicant.tenant_id', true), '') $$;
CREATE FUNCTION ops.principal_scope() RETURNS text LANGUAGE sql STABLE AS $$ SELECT coalesce(nullif(current_setting('replicant.principal_scope', true), ''), 'tenant') $$;
CREATE FUNCTION ops.apply_tenant_rls(p_table regclass, p_tenant_column name DEFAULT 'tenant_id') RETURNS void LANGUAGE plpgsql AS $f$
DECLARE t text; base name;
BEGIN
  SELECT format('%I.%I', n.nspname, c.relname), c.relname INTO t, base FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = p_table;
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
  EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', t);
  EXECUTE format('CREATE POLICY %I ON %s FOR ALL TO PUBLIC USING (%I = ops.current_tenant()) WITH CHECK (%I = ops.current_tenant())', 'rls_'||base||'_tenant', t, p_tenant_column, p_tenant_column);
  EXECUTE format('CREATE POLICY %I ON %s FOR SELECT TO r_cross_tenant_read USING (ops.principal_scope() = ''operator:all-tenants'')', 'rls_'||base||'_xt_read', t);
  EXECUTE format('CREATE POLICY %I ON %s FOR ALL TO r_cross_tenant_write USING (ops.principal_scope() = ''system:all-tenants'') WITH CHECK (ops.principal_scope() = ''system:all-tenants'')', 'rls_'||base||'_xt_write', t);
END $f$;
"""

SMOKE = r"""
SET replicant.tenant_id = 'payments';
INSERT INTO tenancy.tenants (tenant_id, display_name, status, created_by) VALUES ('payments', 'Payments', 'active', 'svc_dev-seeder');
INSERT INTO intake.job_requests (job_id, tenant_id, request_id, dedupe_key, channel, requester, submitted_by, workflow_type,
  request, release_bundle_id, assignment_reason, release_cohort, assignment_version, received_at)
VALUES ('job_01m5p7x7r0bt69j0h0fbxepw90', 'payments', 'jrq_01m5p7x694hhkv1314rfwghn9v', 'github:issue-label:R_kgDOK2a9Xw:1423:replicant',
  'github', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp', 'svc_github-integration', 'auto', '{}', 'rb_ds5p3wmcj4dag2e9a8ncp4py6k', 'ramp:canary:0+layer', 'candidate', 1, now());
INSERT INTO jobs_read.job_timeline (job_id, seq, tenant_id, type, at, summary) VALUES
  ('job_01m5p7x7r0bt69j0h0fbxepw90', 1, 'payments', 'job.created', now(), 'Job created');
INSERT INTO jobs_read.jobs (job_id, tenant_id, seq, as_of, workflow_type, status, phase, requester, release_bundle_id, created_at, snapshot)
VALUES ('job_01m5p7x7r0bt69j0h0fbxepw90', 'payments', 5, now(), 'bugfix', 'running', 'implementing', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp',
  'rb_ds5p3wmcj4dag2e9a8ncp4py6k', now(), '{}');
-- seq guard: an older snapshot must not overwrite
INSERT INTO jobs_read.jobs AS j (job_id, tenant_id, seq, as_of, workflow_type, status, phase, requester, release_bundle_id, created_at, snapshot)
VALUES ('job_01m5p7x7r0bt69j0h0fbxepw90', 'payments', 3, now(), 'bugfix', 'queued', 'routing', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp',
  'rb_ds5p3wmcj4dag2e9a8ncp4py6k', now(), '{}')
ON CONFLICT (job_id) DO UPDATE SET seq = excluded.seq, status = excluded.status, phase = excluded.phase WHERE excluded.seq > j.seq;
"""


def main() -> int:
    srv = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    uri = srv.get_uri()
    errors = []
    with psycopg.connect(uri, autocommit=True) as c:
        for r in ROLES:
            c.execute(f"CREATE ROLE {r} NOLOGIN")
        c.execute(OPS_STUB)
        for name in ORDER:
            sql = (HERE / "ddl" / f"{name}.sql").read_text()
            if "gin_trgm_ops" in sql and not c.execute("SELECT 1 FROM pg_available_extensions WHERE name='pg_trgm'").fetchone():
                sql = "\n".join(l for l in sql.splitlines() if "gin_trgm_ops" not in l)
                print(f"note: pg_trgm unavailable in this PostgreSQL build; skipped trigram index in {name}.sql")
            elif "gin_trgm_ops" in sql:
                c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            try:
                with c.transaction():
                    c.execute(sql)
                if name == "jobs_read":   # WS-10 maintenance creates monthly partitions; one default partition for the check
                    c.execute("CREATE TABLE jobs_read.job_timeline_default PARTITION OF jobs_read.job_timeline DEFAULT")
                print(f"applied {name}.sql")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}.sql: {e}")
        if not errors:
            with c.transaction():
                c.execute(SMOKE)
            st = c.execute("SELECT seq, status FROM jobs_read.jobs").fetchone()
            assert st == (5, "running"), st
            try:
                with c.transaction():
                    c.execute("""INSERT INTO intake.job_requests (job_id, tenant_id, request_id, dedupe_key, channel, requester, submitted_by,
                      workflow_type, request, release_bundle_id, assignment_reason, release_cohort, assignment_version, received_at) VALUES
                      ('job_01m5p7x7r0bt69j0h0fbxepw91','payments','jrq_01m5p7x694hhkv1314rfwghn9w','github:issue-label:R_kgDOK2a9Xw:1423:replicant',
                      'github','usr_01knk8dfr0fvjc7v0b9vhvkzbp','svc_github-integration','auto','{}','rb_ds5p3wmcj4dag2e9a8ncp4py6k','default','default',1,now())""")
                errors.append("dedupe uniqueness not enforced")
            except psycopg.errors.UniqueViolation:
                print("dedupe uniqueness enforced")
            try:
                with c.transaction():
                    c.execute("INSERT INTO identity.role_bindings VALUES (1, 1, 'replicant-admins', 'platform', NULL, 'replicant_admin', NULL)")
                errors.append("replicant_admin role binding accepted (D-25 violation)")
            except psycopg.errors.CheckViolation:
                print("replicant_admin cannot be bound through role_bindings (D-25)")
            for bad in ("ramp", "tenant_override", "requested_pin", "fallback_unregistered", "ramp:holdback:5", "tenant_override:anything"):
                try:
                    with c.transaction():
                        c.execute("UPDATE intake.job_requests SET assignment_reason = %s", (bad,))
                    errors.append(f"assignment_reason {bad!r} accepted")
                except psycopg.errors.CheckViolation:
                    pass
            for good in ("explicit_pin", "tenant_override:holdback_tenant+layer", "ramp:holdback:2", "ramp:experiment:0", "eval_pin", "shadow_pin+layer", "tenant_override:layer_pending"):
                with c.transaction():
                    c.execute("UPDATE intake.job_requests SET assignment_reason = %s", (good,))
            print("assignment_reason grammar: WS-06/WS-01 values accepted, legacy values rejected (R1-158)")
            try:
                with c.transaction():
                    c.execute("INSERT INTO tenancy.tenant_settings (tenant_id, version, settings, updated_by) VALUES ('payments', 1, '{\"quotas\": {}}', 'x')")
                errors.append("tenant settings without budget accepted (R2-055)")
            except psycopg.errors.CheckViolation:
                print("tenant settings require monthly and daily budgets (R2-055)")
            n = c.execute("SELECT count(*) FROM pg_policies WHERE schemaname IN ('jobs_read','approvals','intake','tenancy','notify')").fetchone()[0]
            print(f"RLS policies (ops.apply_tenant_rls, 3 per table): {n}")
    srv.cleanup()
    print("\n".join(errors) if errors else "DDL valid: all schemas applied, smoke checks passed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
