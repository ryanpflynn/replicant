"""Drift check: compare the WS-04 core models with a database migrated from the reviewed DDL.

Run (disposable Postgres with sql/memory.sql and sql/ecosystem.sql applied):

    uv run --with 'sqlalchemy>=2.0,<2.1' --with alembic --with 'pgvector>=0.5' --with 'psycopg[binary]' \
        python models/check_drift.py postgresql+psycopg://postgres:pw@localhost:55432/replicant

Exits non-zero if autogenerate reports any difference for the modelled tables. Mirrors the
WS-10 CI step "models and migrated schema have no drift" (D-26).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from pgvector.sqlalchemy import HALFVEC  # noqa: E402
from sqlalchemy import MetaData, create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql.base import ischema_names  # noqa: E402

from replicant_db.models.ecosystem import EcosystemBase  # noqa: E402
from replicant_db.models.memory import MemoryBase  # noqa: E402

ischema_names.setdefault("halfvec", HALFVEC)  # reflect halfvec columns


def main(url: str) -> int:
    engine = create_engine(url)
    failures = 0
    for base in (MemoryBase, EcosystemBase):
        md: MetaData = base.metadata
        schema = md.schema
        modelled = {t.name for t in md.tables.values()}

        def include_object(obj, name, type_, reflected, compare_to, _modelled=modelled):
            if type_ == "table":
                return name in _modelled
            table = getattr(obj, "table", None)
            return table is None or table.name in _modelled

        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn,
                opts={
                    "include_schemas": True,
                    "include_name": lambda name, type_, parent: (type_ != "schema" or name == schema),
                    "include_object": include_object,
                    "compare_type": True,
                    "compare_server_default": False,
                    "target_metadata": md,
                },
            )
            diffs = compare_metadata(ctx, md)
        print(f"[{schema}] modelled tables: {len(modelled)}; differences: {len(diffs)}")
        for d in diffs:
            print("   ", d)
        failures += len(diffs)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
