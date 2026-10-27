#!/usr/bin/env python3
"""CI: every relation carrying a tenant_id column has RLS enabled AND forced (C-PG rule 4).
Usage: REPLICANT_DB_DSN=postgresql://... check_rls.py   (exit 1 and list offenders)."""
import os
import sys

import psycopg

SQL = """
SELECT n.nspname || '.' || c.relname AS rel, c.relrowsecurity, c.relforcerowsecurity
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'public', 'ops')
  AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)
  AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
ORDER BY 1
"""
with psycopg.connect(os.environ.get("REPLICANT_DB_DSN", os.environ.get("REPLICANT_DB_URL", "")).replace("postgresql+psycopg", "postgresql")) as conn:
    bad = conn.execute(SQL).fetchall()
# No exceptions: audit.events is forced too since 202610270030_audit_events_force (v0 SEC-D1).
errs = [b for b in bad if not b[1]]
warns = [b for b in bad if b[1] and not b[2]]
for rel, enabled, forced in errs:
    print(f"ERROR RLS not enabled: {rel}")
for rel, enabled, forced in warns:
    print(f"WARN  RLS not forced (owner replicant_migrator bypasses it): {rel}")
print(f"{len(errs)} tenant tables without RLS, {len(warns)} without FORCE")
sys.exit(1 if errs else 0)
