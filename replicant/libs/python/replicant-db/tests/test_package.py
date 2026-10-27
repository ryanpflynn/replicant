import pathlib

from alembic.config import Config
from alembic.script import ScriptDirectory

from replicant_db import owner_ddl
from replicant_db._build import BUILT_REVISION

PKG = pathlib.Path(owner_ddl.__file__).parent
SPEC = PKG.parents[4] / "spec"


def _script():
    c = Config()
    c.set_main_option("script_location", str(PKG / "migrations"))
    return ScriptDirectory.from_config(c)


def test_built_revision_is_head():
    assert _script().get_current_head() == BUILT_REVISION


def test_single_linear_history_in_index_order():
    revs = [r.revision for r in reversed(list(_script().walk_revisions()))]
    assert revs[0] == "202610240001_ops" and revs[-3:] == ["202610270020_audit_projects", "202610270030_audit_events_force", "202610270040_memory_v0"]
    assert revs[1:18] == [o.revision for o in owner_ddl.OWNER_FILES]
    assert len(owner_ddl.OWNER_FILES) == 17


def test_owner_files_ship_and_audit_adjustment():
    for o in owner_ddl.OWNER_FILES:
        sql = owner_ddl.owner_sql(o)
        assert owner_ddl.created_objects(sql, o.schema), o.file
    audit = owner_ddl.owner_sql(owner_ddl.BY_REVISION["202610240002_audit"])
    assert "CREATE SCHEMA IF NOT EXISTS audit" not in audit


def test_sql_is_verbatim_copy_of_spec():
    if not SPEC.is_dir():
        return
    src = {"audit.sql": "08-observability/sql/audit.sql", "qa.sql": "05-qa/sql/qa-schema.sql",
           "memory.sql": "04-memory-and-context/sql/memory.sql", "tenancy.sql": "07-control-plane-and-ux/data/ddl/tenancy.sql"}
    for name, rel in src.items():
        assert (PKG / "sql" / "owners" / name).read_bytes() == (SPEC / rel).read_bytes(), name
    for f in ("10-roles-and-grants.sql", "20-ops.sql", "40-eval-metrics-reader.sql"):
        assert (PKG / "sql" / f).read_bytes() == (SPEC / "10-data-platform" / "ddl" / f).read_bytes(), f
