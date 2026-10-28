"""replicant_db.maintenance — `replicant-db maintenance run` (the hourly CronJob; postgres.md §7).

Runs as replicant_migrator under the migrate advisory lock (skips if a migration holds it), so
partition DDL never interleaves with schema migrations. Every task records ops.maintenance_runs.
Tested against WS-04's memory.retrievals / memory.usage_events on PostgreSQL 17.11 (2026-10-24).
"""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _month_floor(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def _record(conn: Connection, task: str, target: str, outcome: str, details: dict) -> None:
    conn.execute(
        text("INSERT INTO ops.maintenance_runs (task, target, finished_at, outcome, details) "
             "VALUES (:t, :g, clock_timestamp(), :o, CAST(:d AS jsonb))"),
        {"t": task, "g": target, "o": outcome, "d": json.dumps(details)},
    )


def partition_create(conn: Connection, today: dt.date) -> list[str]:
    """Create monthly partitions from the current month through `premake` months ahead."""
    created: list[str] = []
    rows = conn.execute(text("SELECT * FROM ops.partitioned_tables")).mappings().all()
    for r in rows:
        kind = conn.execute(text("SELECT c.relkind FROM pg_class c WHERE c.oid = to_regclass(:t)"),
                            {"t": r["table_name"]}).scalar()
        if kind != "p":
            _record(conn, "partition_create", r["table_name"], "skipped",
                    {"reason": "parent table not created yet" if kind is None else "table is not partitioned (proposal pending)"})
            continue
        schema, table = r["table_name"].split(".")
        start = _month_floor(today)
        for i in range(r["premake"] + 1):
            lo, hi = _add_months(start, i), _add_months(start, i + 1)
            name = f"{table}_{lo:%Y_%m}"
            if conn.execute(text("SELECT to_regclass(:n)"), {"n": f"{schema}.{name}"}).scalar():
                continue
            if r["key_kind"] == "ulid":
                bounds = conn.execute(
                    text("SELECT ops.ulid_floor(:p, CAST(:lo AS timestamptz)), ops.ulid_floor(:p, CAST(:hi AS timestamptz))"),
                    {"p": r["ulid_prefix"], "lo": lo.isoformat(), "hi": hi.isoformat()},
                ).one()
                frm, to = bounds
            else:
                frm, to = f"{lo.isoformat()} 00:00:00+00", f"{hi.isoformat()} 00:00:00+00"
            if r["owner_role"] != "replicant_migrator":
                conn.execute(text(f'SET LOCAL ROLE "{r["owner_role"]}"'))
            # Bounds are generated values (dates / ULID floors), never user input.
            conn.execute(text(
                f'CREATE TABLE IF NOT EXISTS "{schema}"."{name}" PARTITION OF "{schema}"."{table}" '
                f"FOR VALUES FROM ('{frm}') TO ('{to}')"))
            # XD-ORCH-04: RLS on the parent does not cover direct reads of the partition.
            conn.execute(text("SELECT ops.secure_partition(CAST(:p AS regclass))"), {"p": f'"{schema}"."{name}"'})
            if r["owner_role"] != "replicant_migrator":
                conn.execute(text("RESET ROLE"))
            created.append(f"{schema}.{name}")
        _record(conn, "partition_create", r["table_name"], "succeeded", {"created": [c for c in created if c.startswith(schema + "." + table)]})
    return created


# Leaf partitions in replicant schemas that are not yet isolated: RLS off or not forced, a privilege held by
# anyone but the owner, or a tenant column without the canonical tenant policy.
_UNSECURED_PARTITIONS = text("""
    SELECT format('%I.%I', n.nspname, c.relname) AS part, pg_get_userbyid(c.relowner) AS owner
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relispartition AND c.relkind = 'r'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'public')
      AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity
           OR EXISTS (SELECT 1 FROM aclexplode(c.relacl) a WHERE a.grantee <> c.relowner)
           OR (EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = c.oid AND attname = 'tenant_id' AND NOT attisdropped)
               AND NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = c.oid AND polname = 'rls_' || c.relname || '_tenant')))
    ORDER BY 1""")


def partition_secure(conn: Connection) -> list[str]:
    """Sweep (every run): isolate every leaf partition, including partitions created by owners' DDL or by
    owner functions such as memory.ensure_tenant_partitions (XD-ORCH-04). Touches only partitions that need
    it, because ALTER TABLE ... ROW LEVEL SECURITY takes an ACCESS EXCLUSIVE lock (5 s lock_timeout)."""
    secured: list[str] = []
    me = conn.execute(text("SELECT current_user")).scalar()
    for part, owner in conn.execute(_UNSECURED_PARTITIONS).all():
        if owner != me:
            conn.execute(text(f'SET LOCAL ROLE "{owner}"'))
        revoked = conn.execute(text("SELECT ops.secure_partition(CAST(:p AS regclass))"), {"p": part}).scalar()
        if owner != me:
            conn.execute(text("RESET ROLE"))
        _record(conn, "partition_secure", part, "succeeded", {"revoked_grantees": revoked})
        secured.append(part)
    return secured


def partition_drop(conn: Connection, today: dt.date, audit_export_verified) -> list[str]:  # noqa: ANN001
    """Detach (CONCURRENTLY is not allowed inside a transaction block, so plain DETACH under a
    5 s lock_timeout, retried next hour on failure) and drop partitions whose upper bound is
    older than retain_months. audit.* requires a verified S3 export of the partition first."""
    dropped: list[str] = []
    rows = conn.execute(text("SELECT * FROM ops.partitioned_tables")).mappings().all()
    for r in rows:
        if conn.execute(text("SELECT c.relkind FROM pg_class c WHERE c.oid = to_regclass(:t)"),
                        {"t": r["table_name"]}).scalar() != "p":
            continue
        schema, table = r["table_name"].split(".")
        cutoff = _add_months(_month_floor(today), -r["retain_months"])
        parts = conn.execute(text("""
            SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent JOIN pg_namespace n ON n.oid = p.relnamespace
            WHERE n.nspname = :s AND p.relname = :t ORDER BY c.relname"""), {"s": schema, "t": table}).scalars().all()
        for name in parts:
            suffix = name[len(table) + 1:]
            try:
                month = dt.datetime.strptime(suffix, "%Y_%m").date()
            except ValueError:
                continue  # not a maintenance-managed partition
            if _add_months(month, 1) > cutoff:
                continue
            if r["table_name"] == "audit.events":
                continue  # dropped only by WS-08's audit.drop_expired_partitions (below; XD-FIX-R1-194)
            if r["export_before_drop"] and not audit_export_verified(r["table_name"], month):
                _record(conn, "partition_drop", f"{schema}.{name}", "skipped", {"reason": "export not verified"})
                continue
            if r["owner_role"] != "replicant_migrator":
                conn.execute(text(f'SET LOCAL ROLE "{r["owner_role"]}"'))
            conn.execute(text(f'ALTER TABLE "{schema}"."{table}" DETACH PARTITION "{schema}"."{name}"'))
            conn.execute(text(f'DROP TABLE "{schema}"."{name}"'))
            if r["owner_role"] != "replicant_migrator":
                conn.execute(text("RESET ROLE"))
            _record(conn, "partition_drop", f"{schema}.{name}", "succeeded", {"cutoff": cutoff.isoformat()})
            dropped.append(f"{schema}.{name}")
    return dropped


def audit_drop(conn: Connection) -> list[str]:
    """XD-FIX-R1-194: audit partitions are dropped only through WS-08's SECURITY DEFINER
    audit.drop_expired_partitions(7), which itself refuses any month whose Object-Locked export row count
    does not match. WS-10 maintenance is the only caller (C-PG rule 3)."""
    if conn.execute(text("SELECT to_regprocedure('audit.drop_expired_partitions(integer)')")).scalar() is None:
        return []
    dropped = list(conn.execute(text("SELECT audit.drop_expired_partitions(7)")).scalars())
    _record(conn, "partition_drop", "audit.events", "succeeded", {"dropped": dropped, "via": "audit.drop_expired_partitions"})
    return dropped


def tenant_partitions(conn: Connection) -> list[str]:
    """XD-FIX-R2-051: every run (and at tenant onboarding) call WS-04's idempotent
    memory.ensure_tenant_partitions(<tenant>) for every tenant; it creates and secures the tenant's
    entry_embeddings LIST partition. Also reports rows in memory.entry_embeddings_default, which must stay
    empty (metric replicant_db_memory_embeddings_default_rows; alert ReplicantDbMemoryEmbeddingsDefaultNotEmpty)."""
    if conn.execute(text("SELECT to_regprocedure('memory.ensure_tenant_partitions(text)')")).scalar() is None:
        return []
    tenants = list(conn.execute(text(
        "SELECT tenant_id FROM tenancy.tenants WHERE offboarded_at IS NULL ORDER BY 1")).scalars()) \
        if conn.execute(text("SELECT to_regclass('tenancy.tenants')")).scalar() else []
    for t in tenants:
        conn.execute(text("SELECT memory.ensure_tenant_partitions(:t)"), {"t": t})
    default_rows = conn.execute(text("SELECT count(*) FROM memory.entry_embeddings_default")).scalar() \
        if conn.execute(text("SELECT to_regclass('memory.entry_embeddings_default')")).scalar() else 0
    _record(conn, "partition_create", "memory.entry_embeddings", "succeeded",
            {"tenants": len(tenants), "default_partition_rows": default_rows})
    return tenants


if __name__ == "__main__":  # integration check used in tools/validate.sh
    import sys

    from replicant_db.session import sync_engine_from_env

    eng = sync_engine_from_env("replicant-db-maintenance")
    today = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    with eng.begin() as c:
        c.execute(text("SET lock_timeout = '5s'"))
        c.execute(text("SELECT set_config('replicant.principal_scope', 'system:all-tenants', true)"))
        print("created:", partition_create(c, today))
        print("tenant partitions:", len(tenant_partitions(c)))
        print("secured:", partition_secure(c))
        print("dropped:", partition_drop(c, today, lambda t, m: False) + audit_drop(c))
