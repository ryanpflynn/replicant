#!/usr/bin/env python3
"""Catalog-snapshot diff between two databases (schema-management.md §5 step 7).

DB-A is built by Alembic migrations, and DB-B by bootstrap plus the reviewed spec DDL files. The
script compares structure only: tables and columns (type, nullability, default, collation),
constraints (name + definition), indexes (definition), RLS flags and policies, triggers,
functions (definition), views, partition parents, and role grants on replicant schemas. Data and
partition children are ignored.

Usage: spec_ddl_equivalence.py <dsn-a> <dsn-b> [--schemas ops,memory,...]
Exit 1 with a unified diff when they differ. (migra is deprecated, hence this tool.)
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys

import psycopg

REPLICANT_SCHEMAS = ["ops", "tenancy", "identity", "intake", "registry", "jobs_read", "approvals", "memory",
                   "ecosystem", "cost", "evals", "audit", "notify", "github_app", "slack_app", "jira_app", "qa"]

Q = {
    "columns": """
        SELECT c.table_schema, c.table_name, c.column_name, c.data_type, c.udt_name, c.is_nullable,
               coalesce(c.column_default, ''), coalesce(c.collation_name, ''), c.ordinal_position
        FROM information_schema.columns c
        JOIN pg_class k ON k.relname = c.table_name
        JOIN pg_namespace n ON n.oid = k.relnamespace AND n.nspname = c.table_schema
        WHERE c.table_schema = ANY(%(s)s) AND NOT k.relispartition AND c.table_name <> 'alembic_version'
        ORDER BY 1, 2, 9""",
    "constraints": """
        SELECT n.nspname, t.relname, c.conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = ANY(%(s)s) AND NOT t.relispartition AND t.relname <> 'alembic_version' ORDER BY 1, 2, 3""",
    "indexes": """
        SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes i
        JOIN pg_class k ON k.relname = i.tablename
        JOIN pg_namespace n ON n.oid = k.relnamespace AND n.nspname = i.schemaname
        WHERE schemaname = ANY(%(s)s) AND NOT k.relispartition AND tablename <> 'alembic_version' ORDER BY 1, 2, 3""",
    "rls": """
        SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(%(s)s) AND c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition
          AND c.relname <> 'alembic_version' ORDER BY 1, 2""",
    "policies": """
        SELECT schemaname, tablename, policyname, permissive, roles::text, cmd, coalesce(qual, ''), coalesce(with_check, '')
        FROM pg_policies WHERE schemaname = ANY(%(s)s) ORDER BY 1, 2, 3""",
    "triggers": """
        SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(%(s)s) AND NOT t.tgisinternal ORDER BY 1, 2, 3""",
    "functions": """
        SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), md5(pg_get_functiondef(p.oid))
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = ANY(%(s)s) AND p.prokind IN ('f', 'p') ORDER BY 1, 2, 3""",
    "views": """
        SELECT schemaname, viewname, md5(definition) FROM pg_views WHERE schemaname = ANY(%(s)s)
        UNION ALL
        SELECT schemaname, matviewname, md5(definition) FROM pg_matviews WHERE schemaname = ANY(%(s)s) ORDER BY 1, 2""",
    "partitioned": """
        SELECT n.nspname, c.relname, pg_get_partkeydef(c.oid)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'p' AND n.nspname = ANY(%(s)s) ORDER BY 1, 2""",
    "grants": """
        SELECT grantee, table_schema, table_name, privilege_type FROM information_schema.role_table_grants
        WHERE table_schema = ANY(%(s)s) AND table_name <> 'alembic_version'
          AND table_name !~ '_[0-9]{4}_[0-9]{2}$' ORDER BY 1, 2, 3, 4""",
}


def snapshot(dsn: str, schemas: list[str]) -> dict[str, list]:
    with psycopg.connect(dsn) as conn:
        return {k: [list(map(str, r)) for r in conn.execute(q, {"s": schemas}).fetchall()] for k, q in Q.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dsn_a")
    ap.add_argument("dsn_b")
    ap.add_argument("--schemas", default=",".join(REPLICANT_SCHEMAS))
    a = ap.parse_args()
    schemas = a.schemas.split(",")
    sa, sb = snapshot(a.dsn_a, schemas), snapshot(a.dsn_b, schemas)
    if sa == sb:
        print(f"equivalent ({sum(len(v) for v in sa.values())} catalog facts compared)")
        return 0
    for k in Q:
        if sa[k] != sb[k]:
            la = [json.dumps(r) for r in sa[k]]
            lb = [json.dumps(r) for r in sb[k]]
            sys.stdout.writelines(l + "\n" for l in difflib.unified_diff(la, lb, f"migrations/{k}", f"spec/{k}", lineterm=""))
    return 1


if __name__ == "__main__":
    sys.exit(main())
