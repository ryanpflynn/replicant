"""Owner DDL as Alembic revisions (spec/10-data-platform/ddl/INDEX.md §2).

Each owner's reviewed `.sql` file ships verbatim as package data (`replicant_db/sql/owners/`) and is applied
by exactly one revision, in the INDEX.md apply order, as `replicant_migrator` (the audit file as `audit_owner`,
with its `CREATE SCHEMA` line dropped because WS-10's roles file already created the schema, exactly as
`tools/apply_owner_ddl.sh` does). Applying the reviewed file itself is what makes "spec DDL == migrations"
hold by construction; `tools/validate_migrations.sh` proves it with the catalog diff.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

from alembic import context, op


@dataclass(frozen=True)
class OwnerFile:
    revision: str
    schema: str
    file: str          # under replicant_db/sql/owners/
    owner_ws: str
    run_as: str = "replicant_migrator"


# INDEX.md §2 apply order (17 owner files). Keep in sync with the revision scripts in migrations/versions.
OWNER_FILES: tuple[OwnerFile, ...] = (
    OwnerFile("202610240002_audit", "audit", "audit.sql", "WS-08", run_as="audit_owner"),
    OwnerFile("202610240003_tenancy", "tenancy", "tenancy.sql", "WS-07"),
    OwnerFile("202610240004_identity", "identity", "identity.sql", "WS-07"),
    OwnerFile("202610240005_registry", "registry", "registry-definitions.sql", "WS-03"),
    OwnerFile("202610240006_registry_releases", "registry", "registry-releases.sql", "WS-06"),
    OwnerFile("202610240007_intake", "intake", "intake.sql", "WS-07"),
    OwnerFile("202610240008_jobs_read", "jobs_read", "jobs_read.sql", "WS-07"),
    OwnerFile("202610240009_approvals", "approvals", "approvals.sql", "WS-07"),
    OwnerFile("202610240010_notify", "notify", "notify.sql", "WS-07"),
    OwnerFile("202610240011_github_app", "github_app", "github_app.sql", "WS-07"),
    OwnerFile("202610240012_slack_app", "slack_app", "slack_app.sql", "WS-07"),
    OwnerFile("202610240013_jira_app", "jira_app", "jira_app.sql", "WS-07"),
    OwnerFile("202610240014_memory", "memory", "memory.sql", "WS-04"),
    OwnerFile("202610240015_ecosystem", "ecosystem", "ecosystem.sql", "WS-04"),
    OwnerFile("202610240016_cost", "cost", "cost.sql", "WS-03"),
    OwnerFile("202610240017_evals", "evals", "evals.sql", "WS-06"),
    OwnerFile("202610240018_qa", "qa", "qa.sql", "WS-05"),
)
BY_REVISION = {o.revision: o for o in OWNER_FILES}


def read_sql(relpath: str) -> str:
    """Package SQL with psql meta-commands (`\\set ...`) removed; everything else verbatim."""
    raw = resources.files("replicant_db").joinpath("sql", relpath).read_text()
    return "\n".join(l for l in raw.splitlines() if not l.lstrip().startswith("\\"))


def owner_sql(o: OwnerFile) -> str:
    sql = read_sql(f"owners/{o.file}")
    if o.run_as == "audit_owner":
        sql = "\n".join(l for l in sql.splitlines() if not l.startswith("CREATE SCHEMA IF NOT EXISTS audit"))
    return sql


def execute(sql: str) -> None:
    """Online: hand the script to the driver untouched (no SQLAlchemy bind-parameter parsing of ':' in the
    reviewed DDL; psycopg runs multi-statement text when no parameters are passed). Offline: render."""
    if context.is_offline_mode():
        op.execute(sql)
        return
    # The raw DBAPI cursor on the migration connection (same transaction). Without parameters psycopg sends
    # the text as-is, so `%I` in format() calls and `:` in literals reach PostgreSQL untouched.
    with op.get_bind().connection.dbapi_connection.cursor() as cur:
        cur.execute(sql)


def upgrade(revision: str) -> None:
    o = BY_REVISION[revision]
    if o.run_as != "replicant_migrator":
        execute(f"SET ROLE {o.run_as}")
    execute(owner_sql(o))
    # env.py runs every revision as replicant_migrator; restore it before ops.migration_log is written.
    execute("SET ROLE replicant_migrator")


_OBJ = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNLOGGED\s+)?(TABLE|VIEW|MATERIALIZED\s+VIEW|FUNCTION|SEQUENCE|TYPE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)",
    re.I,
)


def created_objects(sql: str, schema: str) -> list[tuple[str, str]]:
    """(kind, name) of every object the file creates in its own schema, in file order."""
    seen: dict[tuple[str, str], None] = {}
    stripped = re.sub(r"--[^\n]*", "", sql)
    for m in _OBJ.finditer(stripped):
        kind, sch, name = re.sub(r"\s+", " ", m.group(1).upper()), m.group(2).lower(), m.group(3).lower()
        if sch == schema:
            seen[(kind, name)] = None
    return list(seen)


def downgrade(revision: str) -> None:
    """Drop what the revision's file created (reverse order, CASCADE). Used by the CI round trip on a
    disposable database only; the schema itself and WS-10's USAGE grants stay (they belong to 0001_ops)."""
    o = BY_REVISION[revision]
    objs = created_objects(owner_sql(o), o.schema)
    stmts: list[str] = []
    if o.run_as != "replicant_migrator":
        stmts.append(f"SET ROLE {o.run_as}")
    order = {"VIEW": 0, "MATERIALIZED VIEW": 0, "TABLE": 1, "SEQUENCE": 2, "FUNCTION": 3, "TYPE": 4}
    for kind, name in sorted(reversed(objs), key=lambda kn: order[kn[0]]):
        if kind == "FUNCTION":
            stmts.append(
                "DO $drop$ DECLARE f regprocedure; BEGIN FOR f IN SELECT p.oid::regprocedure FROM pg_proc p "
                f"WHERE p.pronamespace = '{o.schema}'::regnamespace AND p.proname = '{name}' LOOP "
                "EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', f); END LOOP; END $drop$")
        else:
            stmts.append(f'DROP {kind} IF EXISTS "{o.schema}"."{name}" CASCADE')
    stmts.append("SET ROLE replicant_migrator")
    for s in stmts:
        execute(s)
