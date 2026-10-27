"""CI lint (XD-00-01, C-IDS 2.1, ADR-008): every side-effecting activity call passes an explicit ID.

Rules, checked by walking the AST of every module in the worker package:
  L1 workflow.execute_activity / workflow.start_activity / ReplicantJobBase.call with a literal activity name that the
     catalog marks side_effect=True must pass activity_id=...
  L2 NodeContext.act(<literal name>, ...) for a side-effecting activity must pass effect=...
  L3 the activity name must be a string literal that exists in the catalog (no dynamic names), or
     the call must be in the allowlist below.
  L4 effect= literals must match the logical-ID effect grammar ^[a-z][a-z0-9_]{0,30}$.
Exit status 1 on any finding. Usage: python tools/lint_activity_ids.py replicant_orchestration
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import importlib  # noqa: E402
import pkgutil  # noqa: E402

import replicant_orchestration.nodes as _nodes  # noqa: E402
from replicant_orchestration.activity_options import CATALOG  # noqa: E402

for _m in pkgutil.iter_modules(_nodes.__path__):  # node modules may register their own activities
    importlib.import_module(f"replicant_orchestration.nodes.{_m.name}")

EFFECT_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
# Wrapper functions that forward a catalogued name (they themselves enforce side_effect => explicit ID)
DYNAMIC_OK = {("context.py", "act"), ("workflow.py", "_aux_act"), ("workflow.py", "_notify"), ("workflow.py", "call"), ("workflow.py", "start_side_effect")}


def check_file(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []
    enclosing: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(fn):
                enclosing[id(sub)] = fn.name  # innermost def wins (ast.walk visits outer defs first)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        if name not in ("execute_activity", "start_activity", "act", "_aux_act", "call", "start_side_effect"):
            continue
        kw = {k.arg for k in node.keywords}
        first = node.args[0] if node.args else None
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            if (path.name, enclosing.get(id(node), "")) not in DYNAMIC_OK and name not in ("act", "call", "start_side_effect"):
                out.append(f"{path}:{node.lineno}: L3 non-literal activity name in {name}()")
            continue
        act = first.value
        if act not in CATALOG:
            out.append(f"{path}:{node.lineno}: L3 activity {act!r} not in catalog")
            continue
        side = CATALOG[act].side_effect
        if name in ("execute_activity", "start_activity", "call", "start_side_effect") and side and "activity_id" not in kw:
            out.append(f"{path}:{node.lineno}: L1 side-effecting {act} without activity_id=")
        if name == "act" and side and "effect" not in kw:
            out.append(f"{path}:{node.lineno}: L2 side-effecting {act} without effect=")
        for k in node.keywords:
            if k.arg == "effect" and isinstance(k.value, ast.Constant) and not EFFECT_RE.match(k.value.value):
                out.append(f"{path}:{node.lineno}: L4 effect {k.value.value!r} violates the grammar")
    return out


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1] if len(argv) > 1 else "replicant_orchestration")
    findings = [f for p in sorted(root.rglob("*.py")) if "testing" not in p.parts for f in check_file(p)]
    for f in findings:
        print(f)
    print(f"{len(findings)} finding(s) in {root}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
