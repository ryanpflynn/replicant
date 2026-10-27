#!/usr/bin/env python3
"""CI grant checks for database `replicant` (schema-management.md §5 step 8; R1-160 / XD-ORCH-03).

  1. WS-10 grants nothing by default in owner schemas: pg_default_acl may hold, outside `ops`, only
     entries that grant to the object owner (i.e. the REVOKE EXECUTE ... FROM PUBLIC entries).
  2. Login and module-role memberships equal the matrix in ddl/10-roles-and-grants.sql.
  3. Every table privilege held by a replicant login or module role matches C-PG: DML only to the
     schema's writer (control-plane-api writes through svc_cpa_<module>), SELECT only to listed
     readers; grafana_ro only on views or C-PG rule-6 schemas.
  4. Narrowing probes: the owners' narrowing grants actually hold at runtime (column-level token
     secrecy, append-only history). Skipped per probe when the object does not exist.
Also prints the grafana_ro read report (XD-ORCH-03 "grafana_ro grants across ecosystems").
Usage: REPLICANT_DB_DSN=postgresql://... check_grants.py   (exit 1 on any violation)
"""
import os
import pathlib
import re
import sys

import psycopg

ddl = (pathlib.Path(__file__).resolve().parents[1] / "ddl" / "10-roles-and-grants.sql").read_text()
matrix: dict[str, dict] = {}
for login, writes, reads, audit, xtr, xtw in re.findall(
        r"\('(\w+)',\s*'\{([^}]*)\}',\s*'\{([^}]*)\}',\s*(true|false),\s*(true|false),\s*(true|false)\)", ddl):
    matrix[login] = {"writes": {s for s in writes.split(",") if s}, "reads": {s for s in reads.split(",") if s},
                     "audit": audit == "true", "xtr": xtr == "true", "xtw": xtw == "true"}

MODULES = {"svc_cpa_tenancy": "tenancy", "svc_cpa_intake": "intake", "svc_cpa_registry": "registry", "svc_cpa_notify": "notify"}
GRAFANA_SCHEMAS = {"jobs_read", "approvals", "cost", "evals", "ops", "intake", "registry"}   # C-PG rule 6 + §2 table
OWNER_SCHEMAS = ["tenancy", "identity", "intake", "registry", "jobs_read", "approvals", "memory", "ecosystem", "cost",
                 "evals", "audit", "notify", "github_app", "slack_app", "jira_app", "qa"]
BG_READ_SCHEMAS = {"jobs_read", "approvals", "intake", "registry", "cost"}

errors: list[str] = []
with psycopg.connect(os.environ.get("REPLICANT_DB_DSN", os.environ.get("REPLICANT_DB_URL", "")).replace("postgresql+psycopg", "postgresql")) as conn:
    # 1. default privileges
    for nsp, owner, acl in conn.execute("""
        SELECT n.nspname, pg_get_userbyid(d.defaclrole), d.defaclacl::text[]
        FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace
        WHERE n.nspname = ANY(%s)""", (OWNER_SCHEMAS,)):
        for item in acl or []:
            grantee = item.split("=", 1)[0]
            if grantee not in ("", owner):
                errors.append(f"default privilege in owner schema {nsp}: {item} (WS-10 must not grant by default)")

    # 2. memberships
    def members(role: str) -> set[str]:
        return {r for (r,) in conn.execute(
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s", (role,))}
    for login, m in matrix.items():
        want = {"r_ops_read"} | ({"rds_iam"} if login != "grafana_ro" else set())
        want |= {"r_audit_emit"} if m["audit"] else set()
        want |= {"r_cross_tenant_read"} if m["xtr"] else set()
        want |= {"r_cross_tenant_write"} if m["xtw"] else set()
        if login == "svc_control_plane_api":
            want |= set(MODULES) | {"svc_cpa_read"}
        elif login != "grafana_ro":
            want |= {f"r_{s}_read" for s in m["reads"] if s != "audit"} | {f"r_{s}_write" for s in m["writes"]}
        if login == "svc_audit_exporter":
            want |= {"audit_reader"}
        have = members(login)
        if have != want:
            errors.append(f"{login}: missing {sorted(want - have)} extra {sorted(have - want)}")

    # 3. C-PG compliance of every privilege on owner tables held by replicant roles
    rows = conn.execute("""
        SELECT g.grantee, g.table_schema, g.table_name, g.privilege_type, c.relkind
        FROM information_schema.role_table_grants g
        JOIN pg_namespace n ON n.nspname = g.table_schema
        JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = g.table_name
        WHERE g.table_schema = ANY(%s) AND (g.grantee LIKE 'svc\\_%%' OR g.grantee LIKE 'r\\_%%'
                                             OR g.grantee IN ('grafana_ro','bg_readonly'))""",
        (OWNER_SCHEMAS,)).fetchall()
    grafana_report: dict[str, set[str]] = {}
    for grantee, schema, table, priv, kind in rows:
        cap = re.fullmatch(r"r_(.+)_(read|write)", grantee)
        if cap:
            ok = cap.group(1) == schema and (cap.group(2) == "write" or priv == "SELECT")
        elif grantee == "bg_readonly":
            ok = priv == "SELECT" and schema in BG_READ_SCHEMAS
        elif grantee == "grafana_ro":
            grafana_report.setdefault(schema, set()).add(table + (" (view)" if kind in ("v", "m") else ""))
            ok = priv == "SELECT" and (schema in GRAFANA_SCHEMAS or kind in ("v", "m"))
        elif grantee in MODULES:
            ok = (MODULES[grantee] == schema) or priv == "SELECT"
        elif grantee == "svc_cpa_read":
            ok = priv == "SELECT" and schema in matrix["svc_control_plane_api"]["reads"]
        elif grantee in matrix:
            m = matrix[grantee]
            if grantee == "svc_control_plane_api" and priv != "SELECT":
                ok = False   # control-plane-api writes only through its module roles
            else:
                ok = schema in m["writes"] if priv != "SELECT" else schema in (m["reads"] | m["writes"])
        else:
            ok = False
        if not ok:
            errors.append(f"C-PG violation: {priv} on {schema}.{table} to {grantee}")

    # 3b. eval_metrics_reader (CCR-06-15): column privileges only, only on the allowlist (ddl/40-*).
    EVAL_COLUMNS = {
        ("jobs_read", "jobs"): {"job_id", "tenant_id", "workflow_type", "status", "is_terminal", "phase", "evaluation_mode",
                                "release_bundle_id", "release_cohort", "migrated", "worker_build_id", "memory_snapshot_id",
                                "cycle", "repair_used", "repair_max", "risk_level", "terminal_outcome", "terminal_at",
                                "created_at"},
        ("jobs_read", "job_prs"): {"job_id", "tenant_id", "repo_id", "pr_number", "state", "job_open", "opened_at",
                                   "merged_at", "closed_at"},
        ("intake", "job_requests"): {"job_id", "tenant_id", "workflow_type", "evaluation_mode", "release_bundle_id",
                                     "assignment_reason", "release_cohort", "start_status", "received_at", "started_at",
                                     "terminal_at"},
        ("approvals", "gates"): {"gate_id", "job_id", "tenant_id", "kind", "state", "risk_level", "release_bundle_id",
                                 "opened_at", "expires_at", "escalation_level", "packet_revision", "presented_digest",
                                 "min_approvers", "approvals_recorded", "decided_at"},
        ("jobs_read", "job_runs"): {"run_id", "job_id", "tenant_id", "node_key", "cycle", "ordinal", "role", "phase",
                                    "reason", "result_outcome", "qa_verdict", "started_at", "finished_at"},
        ("jobs_read", "job_memory_usage"): {"job_id", "tenant_id", "memory_snapshot_id"},
        ("memory", "v_job_outcomes"): {"job_id", "tenant_id", "release_bundle_id", "workflow_type", "terminal_status",
                                       "pr_merged", "merged_at", "post_merge_reverted", "outcome_report_count",
                                       "review_rounds", "first_pass_qa", "first_review_approved", "report_kind",
                                       "reported_at"},
        # XD-W2-10-07: base columns read through the security_invoker view memory.v_job_outcomes
        ("memory", "job_outcomes"): {"job_id", "tenant_id", "release_bundle_id", "workflow_type", "terminal_status",
                                     "pr_merged", "merged_at", "post_merge_reverted", "outcome_report_count",
                                     "review_rounds", "first_pass_qa", "first_review_approved"},
        ("memory", "job_outcome_reports"): {"job_id", "tenant_id", "kind", "created_at"},
        ("cost", "usage_events"): {"job_id", "tenant_id", "served_model", "occurred_at"},   # XD-W3-R1V-036
        ("cost", "job_costs"): {"job_id", "tenant_id", "llm_cost_micros", "worker_llm_cost_micros", "compute_cost_micros",
                                "budget_micros", "extensions", "updated_at"},
    }
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'eval_metrics_reader'").fetchone():
        for schema, table, priv in conn.execute(
                "SELECT table_schema, table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'eval_metrics_reader'"):
            errors.append(f"eval_metrics_reader holds table-level {priv} on {schema}.{table} (column grants only)")
        for schema, table, column, priv in conn.execute(
                "SELECT table_schema, table_name, column_name, privilege_type FROM information_schema.column_privileges "
                "WHERE grantee = 'eval_metrics_reader'"):
            if priv != "SELECT" or column not in EVAL_COLUMNS.get((schema, table), set()):
                errors.append(f"eval_metrics_reader: {priv} on {schema}.{table}.{column} is outside the CCR-06-15 allowlist")

    # 3c. audit.emit must be executable through r_audit_emit (C-PG rule 3), or no service can audit.
    if conn.execute("SELECT to_regprocedure('audit.emit(jsonb)')").fetchone()[0] is not None:
        if not conn.execute("SELECT has_function_privilege('r_audit_emit', 'audit.emit(jsonb)', 'EXECUTE')").fetchone()[0]:
            errors.append("r_audit_emit lacks EXECUTE on audit.emit(jsonb): no writer can emit audit events")

    # 3d. Marker roles are pure scope markers: no table or column privilege, no owned relation (postgres.md §3.1a).
    MARKERS = ["r_cross_tenant_read", "r_cross_tenant_write", "r_audit_emit", "r_audit_restore"]
    for mk in MARKERS:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (mk,)).fetchone():
            errors.append(f"marker role {mk} is missing")
            continue
        for schema, table, priv in conn.execute(
                "SELECT table_schema, table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = %s", (mk,)):
            errors.append(f"marker {mk} holds {priv} on {schema}.{table} (markers hold no table privileges)")
        for schema, table, column, priv in conn.execute(
                "SELECT table_schema, table_name, column_name, privilege_type FROM information_schema.column_privileges "
                "WHERE grantee = %s", (mk,)):
            errors.append(f"marker {mk} holds {priv} on {schema}.{table}.{column} (markers hold no column privileges)")
        for (rel,) in conn.execute("SELECT c.oid::regclass::text FROM pg_class c WHERE c.relowner = %s::regrole", (mk,)):
            errors.append(f"marker {mk} owns {rel}")

    # 3e. audit-exporter's approval-record reconciler (XD-W2-08-03): column read access it needs.
    AUDIT_EXPORTER_NEEDS = {
        "approvals.gates": ["gate_id", "job_id", "tenant_id", "packet_key", "packet_revision", "presented_digest",
                            "memory_refs"],
        "approvals.gate_decisions": ["decision_id", "gate_id", "tenant_id", "outcome", "decision", "principal_id",
                                     "channel", "delegate_of", "text_key", "text_sha256", "decided_at",
                                     "presented_digest"],
    }
    for table, cols in AUDIT_EXPORTER_NEEDS.items():
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            errors.append(f"audit-exporter probe: {table} does not exist")
            continue
        for col in cols:
            if not conn.execute("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s "
                                "AND NOT attisdropped)", (table, col)).fetchone()[0]:
                errors.append(f"audit-exporter probe: column {table}.{col} does not exist")
            elif not conn.execute("SELECT has_column_privilege('svc_audit_exporter', %s, %s, 'SELECT')",
                                  (table, col)).fetchone()[0]:
                errors.append(f"audit-exporter cannot read {table}.{col}")
        for priv in ("INSERT", "UPDATE", "DELETE"):
            if conn.execute("SELECT has_table_privilege('svc_audit_exporter', %s, %s)", (table, priv)).fetchone()[0]:
                errors.append(f"audit-exporter can {priv} {table} (read-only expected)")

    # 4. narrowing probes (owners' authoritative restrictions must survive)
    roles = [r for r in list(matrix) + list(MODULES) + ["svc_cpa_read", "memory_analytics_owner", "qa_analytics_owner",
                                                          "eval_metrics_reader", "bg_readonly"]]
    probes = [
        ("identity.sessions", "refresh_token_enc", "SELECT", {"svc_identity_service"}),
        ("identity.link_flows", "pkce_verifier_enc", "SELECT", {"svc_identity_service"}),
    ]
    for table, column, priv, allowed in probes:
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            continue
        if not conn.execute("SELECT 1 FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s",
                            (table, column)).fetchone():
            continue
        for r in roles:
            if r not in allowed and conn.execute("SELECT has_column_privilege(%s, %s, %s, %s)",
                                                 (r, table, column, priv)).fetchone()[0]:
                errors.append(f"narrowing voided: {r} can {priv} {table}.{column}")
    for table in ("registry.bundle_promotion_events", "registry.bundle_assignment_history",
                  "memory.state_transitions", "memory.curator_actions"):
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            continue
        for r in roles:
            for priv in ("UPDATE", "DELETE"):
                if conn.execute("SELECT has_table_privilege(%s, %s, %s)", (r, table, priv)).fetchone()[0]:
                    errors.append(f"append-only voided: {r} can {priv} {table}")

for e in sorted(set(errors)):
    print(e)
print("grafana_ro reads:", "; ".join(f"{s}: {len(t)} ({', '.join(sorted(t)[:4])}{'…' if len(t) > 4 else ''})"
                                    for s, t in sorted(grafana_report.items())) or "none")
print("grants OK" if not errors else f"{len(set(errors))} grant problems")
sys.exit(1 if errors else 0)
