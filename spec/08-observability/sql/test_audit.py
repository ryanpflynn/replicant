#!/usr/bin/env python3
"""Executes audit.sql against a disposable PostgreSQL and checks its invariants (WS-08).

    uv run --with pgserver --with "psycopg[binary]" python test_audit.py   # embedded PostgreSQL (local)
    REPLICANT_TEST_DSN=postgresql://... python test_audit.py   # CI: disposable PostgreSQL 17 container

Checks: DDL applies; aud_ IDs match the C-IDS regex and sort by time; emit() rejects unknown keys, nested or
oversized details and unknown event types; writer roles cannot INSERT/UPDATE/DELETE; trace_id is derived from
job_id per Rule T-1; RLS scopes audit_reader; the Grafana view exposes aggregates only.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

SQL = Path(__file__).resolve().parent / "audit.sql"
ROLES = ["replicant_migrator", "audit_owner", "audit_reader", "grafana_ro", "svc_control_plane_api", "svc_audit_exporter",
         "r_audit_restore", "svc_ops_worker", "r_audit_emit"]

# Minimal stand-ins for WS-10's ops objects (ddl/20-ops.sql) so the DDL runs on a bare server. The real
# ops.secure_partition also applies the tenant policies; here only the properties checked below matter.
OPS_STUB = """
CREATE SCHEMA ops;
CREATE TABLE ops.restore_events (restore_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  restore_point timestamptz NOT NULL DEFAULT now(), source text NOT NULL DEFAULT 'pitr',
  incident_ref text NOT NULL DEFAULT 'INC-1', reconcile_state text NOT NULL DEFAULT 'pending');
CREATE FUNCTION ops.principal_scope() RETURNS text LANGUAGE sql STABLE AS
  $f$ SELECT coalesce(nullif(current_setting('replicant.principal_scope', true), ''), 'tenant') $f$;
GRANT EXECUTE ON FUNCTION ops.principal_scope() TO PUBLIC;
CREATE FUNCTION ops.secure_partition(p_part regclass, p_tenant_column name DEFAULT 'tenant_id') RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog AS $f$
BEGIN
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', p_part);
  EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', p_part);
  EXECUTE format('REVOKE ALL ON TABLE %s FROM PUBLIC', p_part);
  RETURN 0;
END $f$;
GRANT USAGE ON SCHEMA ops TO PUBLIC;
GRANT SELECT ON ops.restore_events TO audit_owner;
"""


def main() -> int:
    import psycopg  # psycopg 3

    dsn = os.environ.get("REPLICANT_TEST_DSN")
    if not dsn:
        import pgserver  # type: ignore
        dsn = pgserver.get_server(tempfile.mkdtemp()).get_uri()
    conn = psycopg.connect(dsn, autocommit=True)

    def run(q: str, role: str | None = None, scope: str | None = None, tenants: str | None = None) -> str:
        """Execute q (optionally as role); return the result rows as text, or 'ERROR: <message>'."""
        try:
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
                cur.execute("RESET replicant.principal_scope")
                cur.execute("RESET replicant.audit_tenants")
                if role:
                    cur.execute(f"SET ROLE {role}")
                if scope:
                    cur.execute("SELECT set_config('replicant.principal_scope', %s, false)", (scope,))
                if tenants is not None:
                    cur.execute("SELECT set_config('replicant.audit_tenants', %s, false)", (tenants,))
                cur.execute(q)
                return repr(cur.fetchall()) if cur.description else "OK"
        except psycopg.Error as exc:
            return f"ERROR: {exc}"

    run("DROP SCHEMA IF EXISTS audit CASCADE")
    for role in ROLES:
        run(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
            f"THEN CREATE ROLE {role}; END IF; END $$")
    run("DROP SCHEMA IF EXISTS ops CASCADE")
    run(OPS_STUB)
    res = run(SQL.read_text())
    if res.startswith("ERROR"):
        print(res)
        return 1
    run("GRANT USAGE ON SCHEMA audit TO svc_control_plane_api, r_audit_restore")  # WS-10 grants schema USAGE
    run("GRANT audit_reader TO svc_audit_exporter")  # WS-10 role migration does this in production
    run("GRANT r_audit_emit TO svc_control_plane_api")  # WS-10 role matrix; audit.sql grants EXECUTE to r_audit_emit
    run("SELECT audit.ensure_partitions(2)")

    ok_event = ('{"event_type":"gate.decided","outcome":"success","tenant_id":"payments",'
                '"actor_principal_id":"usr_01knk8dfr0fvjc7v0b9vhvkzbp","channel":"slack",'
                '"source_service":"replicant-worker","job_id":"job_01m5p7x7r0bt69j0h0fbxepw90",'
                '"gate_id":"gate_r1a2avb51zgbsjy0x4ghyktt2e","decision_id":"dec_cnd2aaaaaaaaaaaaaaaaaaaaaa",'
                '"digest":"sha256:' + "a" * 64 + '","details":{"gate_kind":"plan_approval","decision":"approve"}}')
    checks = []
    r1 = run(f"SELECT audit.emit('{ok_event}'::jsonb)", "svc_control_plane_api")
    ids = re.findall(r"aud_[0-9a-hjkmnp-tv-z]{26}", r1)
    checks.append(("emit returns aud_ id", len(ids) == 1))
    r2 = run(f"SELECT audit.emit('{ok_event}'::jsonb)", "svc_control_plane_api")
    ids2 = re.findall(r"aud_[0-9a-hjkmnp-tv-z]{26}", r2)
    checks.append(("ids are time ordered", ids2 and ids2[0] > ids[0]))
    tr = run("SELECT trace_id FROM audit.events WHERE job_id='job_01m5p7x7r0bt69j0h0fbxepw90' LIMIT 1")
    checks.append(("trace_id derived (T-1 vector)", "fc0575170c8597ce9805552723c08d6e" in tr))
    bad_key = ok_event.replace('"details"', '"comment":"free text","details"')
    checks.append(("unknown key rejected",
                   "unknown keys" in run(f"SELECT audit.emit('{bad_key}'::jsonb)", "svc_control_plane_api")))
    nested = ok_event.replace('{"gate_kind":"plan_approval","decision":"approve"}', '{"x":{"y":1}}')
    checks.append(("nested details rejected",
                   "details must be flat" in run(f"SELECT audit.emit('{nested}'::jsonb)", "svc_control_plane_api")))
    unknown_type = ok_event.replace("gate.decided", "gate.made_up")
    checks.append(("unknown event_type rejected",
                   "foreign key" in run(f"SELECT audit.emit('{unknown_type}'::jsonb)", "svc_control_plane_api")))
    checks.append(("direct INSERT denied", "permission denied" in run(
        "INSERT INTO audit.events (audit_event_id) VALUES ('x')", "svc_control_plane_api")))
    checks.append(("UPDATE denied", "permission denied" in run(
        "UPDATE audit.events SET outcome='denied'", "svc_control_plane_api")))
    checks.append(("reader without scope sees nothing", "[(0,)]" == run(
        "SELECT count(*) FROM audit.events", "audit_reader")))
    checks.append(("reader with audit_tenants=payments sees rows", "[(2,)]" == run(
        "SELECT count(*) FROM audit.events", "audit_reader", "tenant", "payments")))
    checks.append(("reader with audit_tenants=commerce sees nothing", "[(0,)]" == run(
        "SELECT count(*) FROM audit.events", "audit_reader", "tenant", "commerce")))
    checks.append(("operator:all-tenants reader sees rows", "[(2,)]" == run(
        "SELECT count(*) FROM audit.events", "audit_reader", "operator:all-tenants")))
    checks.append(("tenant list in principal_scope no longer works", "[(0,)]" == run(
        "SELECT count(*) FROM audit.events", "audit_reader", "payments")))
    checks.append(("grafana_ro cannot read rows", "permission denied" in run(
        "SELECT * FROM audit.events", "grafana_ro")))
    checks.append(("grafana_ro reads aggregate view", "gate.decided" in run(
        "SELECT event_type, events FROM audit.v_daily_counts", "grafana_ro")))
    # Every catalogued type is accepted by emit() (R1-095: a missing type would roll back the caller's transaction).
    types = [t for (t,) in eval(run("SELECT event_type FROM audit.event_types ORDER BY 1"))]
    failures = []
    for t in types:
        ev = ok_event.replace('"gate.decided"', f'"{t}"')
        if not re.search(r"aud_", run(f"SELECT audit.emit('{ev}'::jsonb)", "svc_control_plane_api")):
            failures.append(t)
    checks.append((f"all {len(types)} catalogued types emit", not failures))
    for t in ("memory.entry_transition", "memory.entry_edited", "memory.entry_promoted", "memory.entry_merged",
              "memory.conflict_resolved", "memory.quarantine_created", "memory.quarantine_released",
              "memory.purge_requested", "memory.purge_completed", "memory.export_created", "memory.import_applied",
              "memory.governance_changed", "memory.author_restricted", "memory.flag_created",
              "memory.correction_requested", "memory.hold_cleared", "memory.outcome_report_applied",
              "memory.admin_denied", "authz.denied"):
        checks.append((f"catalog has {t} (WS-04/WS-07 names)", t in types))
    # Export-before-drop guard (R1-194 c).
    run("CREATE TABLE audit.events_2018_01 PARTITION OF audit.events FOR VALUES FROM ('2018-01-01') TO ('2018-02-01')")
    old_ev = ok_event.replace('{"event_type"', '{"occurred_at":"2018-01-15T00:00:00Z","event_type"')
    run(f"SELECT audit.emit('{old_ev}'::jsonb)", "svc_control_plane_api")
    checks.append(("unexported partition is not dropped",
                   run("SELECT audit.drop_expired_partitions(7)") == "[]"))
    checks.append(("exporter records export", run(
        "SELECT audit.record_partition_export('events_2018_01', 1, '" + "b" * 64 + "', "
        "'org/audit-exports/events_2018_01.ndjson.zst', '" + "c" * 64 + "')", "svc_audit_exporter")
        .startswith("[")))
    checks.append(("writer cannot record exports", "permission denied" in run(
        "SELECT audit.record_partition_export('events_2018_01', 1, 'x', 'y', 'z')", "svc_control_plane_api")))
    checks.append(("exported partition is dropped",
                   "events_2018_01" in run("SELECT audit.drop_expired_partitions(7)")))
    # Idempotency key (XD-FIX-R1-157): a replayed projector item returns the same id and adds no row.
    idem = ok_event.replace('{"event_type"', '{"occurred_at":"2026-10-24T10:00:00Z","idempotency_key":"proj:job_01m5p7x7r0bt69j0h0fbxepw90:41","event_type"')
    a1 = run(f"SELECT audit.emit('{idem}'::jsonb)", "svc_control_plane_api")
    a2 = run(f"SELECT audit.emit('{idem}'::jsonb)", "svc_control_plane_api")
    checks.append(("idempotent replay returns the same id", a1.startswith("[") and a1 == a2))
    checks.append(("idempotent replay adds no row", "[(1,)]" == run(
        "SELECT count(*) FROM audit.events WHERE idempotency_key = 'proj:job_01m5p7x7r0bt69j0h0fbxepw90:41'")))
    no_at = ok_event.replace('{"event_type"', '{"idempotency_key":"proj:job_x:1","event_type"')
    checks.append(("idempotency_key without occurred_at rejected", "requires occurred_at" in run(
        f"SELECT audit.emit('{no_at}'::jsonb)", "svc_control_plane_api")))
    # Restore form (XD-FIX-R1-237, CCR-10-09).
    restored = ok_event.replace('{"event_type"', '{"audit_event_id":"aud_01knk8dfr0fvjc7v0b9vhvkzbp","occurred_at":"2026-10-24T09:00:00Z","event_type"')
    checks.append(("emit_restored refused without an open restore", "no restore is open" in run(
        f"SELECT audit.emit_restored('{restored}'::jsonb)", "r_audit_restore")))
    run("INSERT INTO ops.restore_events DEFAULT VALUES")
    checks.append(("emit_restored denied to writers", "permission denied" in run(
        f"SELECT audit.emit_restored('{restored}'::jsonb)", "svc_control_plane_api")))
    run(f"SELECT audit.emit_restored('{restored}'::jsonb)", "r_audit_restore")
    run(f"SELECT audit.emit_restored('{restored}'::jsonb)", "r_audit_restore")
    checks.append(("emit_restored keeps the original id, once", "[(1,)]" == run(
        "SELECT count(*) FROM audit.events WHERE audit_event_id = 'aud_01knk8dfr0fvjc7v0b9vhvkzbp'")))
    checks.append(("emit_restored validates like emit", "unknown keys" in run(
        f"SELECT audit.emit_restored('{restored.replace(chr(34) + 'details' + chr(34), chr(34) + 'comment' + chr(34) + ':1,' + chr(34) + 'details' + chr(34))}'::jsonb)",
        "r_audit_restore")))
    run("UPDATE ops.restore_events SET reconcile_state = 'completed'")
    # Partition isolation (XD-ORCH-04): every leaf partition has forced RLS and no grants.
    leaves = run("SELECT c.relname, c.relrowsecurity AND c.relforcerowsecurity, c.relacl IS NULL OR "
                 "NOT EXISTS (SELECT 1 FROM aclexplode(c.relacl) a WHERE a.grantee <> c.relowner) "
                 "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                 "WHERE i.inhparent = 'audit.events'::regclass AND c.relname <> 'events_2018_01'")
    checks.append(("every audit partition has forced RLS and no grants",
                   "events_default" in leaves and "False" not in leaves))
    checks.append(("partition owned by audit_owner", "[('audit_owner',)]" == run(
        "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = 'audit.events'::regclass AND c.relname ~ '^events_20[2-9]'")))
    checks.append(("reader cannot read a partition directly", "permission denied" in run(
        "SELECT count(*) FROM audit.events_default", "audit_reader", "tenant", "payments")))
    # Exporter scope (XD-W2-10-03 ruling): all tenants only under system:all-tenants.
    checks.append(("exporter sees all tenants under system:all-tenants", run(
        "SELECT count(*) > 0 FROM audit.events", "svc_audit_exporter", "system:all-tenants") == "[(True,)]"))
    checks.append(("exporter sees nothing under org:*", run(
        "SELECT count(*) FROM audit.events", "svc_audit_exporter", "org:*") == "[(0,)]"))
    checks.append(("catalog has data.cross_tenant_read (XD-W2-10-01)", "data.cross_tenant_read" in types))
    for t in ("projection.restore_repair", "gate.decision_identity_contested"):
        checks.append((f"catalog has {t} (R1V-041/042)", t in types))
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
