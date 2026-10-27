#!/usr/bin/env python3
"""CI: revision conventions (schema-management.md §5 step 2). Usage: lint_migrations.py <versions dir>."""
import ast
import pathlib
import re
import sys

SCHEMAS = {"ops", "tenancy", "identity", "intake", "registry", "jobs_read", "approvals", "memory", "ecosystem",
           "cost", "evals", "audit", "notify", "github_app", "slack_app", "jira_app", "qa", "multi"}
COMPAT = {"expand", "contract", "data", "reference", "grants"}   # C-PG 1.2.0 rule 8
errors = []
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.py")):
    mod = ast.parse(f.read_text())
    consts = {t.id: n.value.value for n in mod.body if isinstance(n, (ast.Assign, ast.AnnAssign))
              for t in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets)
              if isinstance(t, ast.Name) and isinstance(getattr(n, "value", None), ast.Constant)}
    rev, schema, compat = consts.get("revision"), consts.get("replicant_schema"), consts.get("replicant_compat")
    if not rev or not re.fullmatch(r"\d{12}_[a-z_]{2,19}", rev) or len(rev) > 32:
        errors.append(f"{f.name}: bad revision id {rev!r}")
    if schema not in SCHEMAS:
        errors.append(f"{f.name}: replicant_schema {schema!r}")
    if compat not in COMPAT:
        errors.append(f"{f.name}: replicant_compat {compat!r}")
    if compat == "contract" and not consts.get("replicant_requires_reader_revision"):
        errors.append(f"{f.name}: contract without replicant_requires_reader_revision")
    src = f.read_text()
    if re.search(r"\bSET\s+(LOCAL\s+)?replicant\.", src, re.I):
        errors.append(f"{f.name}: use set_config(...) for replicant.* GUCs, never SET")
    if schema not in (None, "multi", "ops"):
        for m in re.finditer(r"(?:TABLE|INDEX\s+\S+\s+ON|INTO|UPDATE|FROM|TRIGGER\s+\S+\s+ON\S*)\s+([a-z_]+)\.", src, re.I):
            if m.group(1).lower() not in (schema, "ops", "pg_catalog", "public", "old", "new", "excluded"):
                errors.append(f"{f.name}: touches schema {m.group(1)!r} but replicant_schema={schema!r}")
    if "autocommit_block" in src and re.search(r"CREATE\s+INDEX\s+CONCURRENTLY\s+(?!IF NOT EXISTS)", src, re.I):
        errors.append(f"{f.name}: CONCURRENTLY index inside autocommit_block must use IF NOT EXISTS")
for e in errors:
    print(e)
print("migrations lint OK" if not errors else f"{len(errors)} problems")
sys.exit(1 if errors else 0)
