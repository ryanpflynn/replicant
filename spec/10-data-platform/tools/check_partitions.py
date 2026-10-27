#!/usr/bin/env python3
"""Partition-level isolation check (XD-ORCH-04). RLS on a partitioned parent is not applied when a
partition is queried directly, so every leaf partition must be isolated on its own.

  1. Structural, every leaf partition in Replicant schemas: RLS enabled and forced; no privilege held by
     anyone but the owner; a partition with `tenant_id` carries the canonical `rls_<partition>_tenant` policy.
  2. Behavioral: for every leaf partition with `tenant_id` and every service role (matrix logins, module
     roles, view owners, grafana_ro, bg_readonly), query the partition DIRECTLY as that role with tenant
     scope set to a probe tenant. The result must be "permission denied" or zero rows of other tenants.
     Rows are counted as the superuser first, so the report says how many partitions held real data.

Usage: REPLICANT_DB_DSN=postgresql://<superuser>@.../replicant check_partitions.py   (exit 1 on any violation)
"""
import os
import pathlib
import re
import sys

import psycopg
from psycopg import sql

ddl = (pathlib.Path(__file__).resolve().parents[1] / "ddl" / "10-roles-and-grants.sql").read_text()
LOGINS = re.findall(r"\('(\w+)',\s*'\{[^}]*\}',\s*'\{[^}]*\}',\s*(?:true|false)", ddl)
ROLES = LOGINS + ["svc_cpa_tenancy", "svc_cpa_intake", "svc_cpa_registry", "svc_cpa_notify", "svc_cpa_read",
                  "memory_analytics_owner", "qa_analytics_owner", "bg_readonly"]
PROBE = "zz-probe-tenant"

errors: list[str] = []
dsn = os.environ.get("REPLICANT_DB_DSN", "").replace("postgresql+psycopg", "postgresql")
with psycopg.connect(dsn, autocommit=True) as conn:
    parts = conn.execute("""
        SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity,
               EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = c.oid AND attname = 'tenant_id' AND NOT attisdropped),
               EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = c.oid AND polname = 'rls_' || c.relname || '_tenant'),
               (SELECT string_agg(DISTINCT pg_get_userbyid(a.grantee), ',') FROM aclexplode(c.relacl) a
                 WHERE a.grantee <> c.relowner)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relispartition AND c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog','information_schema','public')
        ORDER BY 1, 2""").fetchall()
    roles = [r for r in ROLES if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (r,)).fetchone()]
    probed = with_data = 0
    for nsp, rel, rls, forced, has_tenant, has_policy, grantees in parts:
        name = f"{nsp}.{rel}"
        if not (rls and forced):
            errors.append(f"partition {name}: RLS enabled={rls} forced={forced}")
        if grantees:
            errors.append(f"partition {name}: direct privileges held by {grantees} (only the owner may hold any)")
        if has_tenant and not has_policy:
            errors.append(f"partition {name}: no canonical tenant policy (ops.secure_partition not applied)")
        if not has_tenant:
            continue
        ident = sql.Identifier(nsp, rel)
        n_other = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE tenant_id <> %s").format(ident), (PROBE,)).fetchone()[0]
        with_data += n_other > 0
        for role in roles:
            if not conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, name)).fetchone()[0]:
                continue  # denied: the expected state after ops.secure_partition
            probed += 1
            with conn.transaction(force_rollback=True):
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                conn.execute("SELECT set_config('replicant.tenant_id', %s, true), "
                             "set_config('replicant.principal_scope', 'tenant', true)", (PROBE,))
                try:
                    leaked = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE tenant_id <> %s").format(ident),
                                          (PROBE,)).fetchone()[0]
                except psycopg.errors.InsufficientPrivilege:
                    leaked = 0
            if leaked:
                errors.append(f"LEAK: {role} reads {leaked} other-tenant rows directly from partition {name}")

for e in errors:
    print(e)
print(f"{len(parts)} leaf partitions, {sum(1 for p in parts if p[4])} with tenant_id, {with_data} holding rows; "
      f"{probed} role/partition pairs had SELECT and were probed")
print("partitions OK" if not errors else f"{len(errors)} partition problems")
sys.exit(1 if errors else 0)
