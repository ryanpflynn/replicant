"""Replicant Guard Language (FGL v1): the expression language of GraphDefinition guards.

Guards appear in `when:` (node presence) and in edge `switch:` branches. FGL is a strict
subset of Python expression syntax, parsed with `ast` and evaluated by a whitelist walker:

* literals: str, int, float, True, False, None, list and tuple displays
* names: the top-level guard context keys (see GUARD_ROOTS)
* attribute access (`risk.level`) and constant subscripts (`nodes['qa'].passed`): dict lookups
  that are null-safe (a missing key yields None, never an exception)
* `and`, `or`, `not`; comparisons `== != < <= > >= in not in`; conditional expressions are NOT allowed
* calls only to the functions in FUNCTIONS
* at most 64 AST nodes and 512 characters

FGL is total (every well-formed guard evaluates to a value), deterministic (no clock, randomness,
or I/O), and side-effect free, so it is safe inside workflow code. Changing FGL semantics is CC-2
"code": it ships with a replicant-worker build, and the NodeCatalog advertises `fglVersion`.
"""

from __future__ import annotations

import ast
import fnmatch
from typing import Any, Callable

FGL_VERSION = 1
MAX_NODES = 64
MAX_CHARS = 512

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

GUARD_ROOTS = frozenset(
    {"job", "request", "risk", "nodes", "gates", "cycle", "params", "policy", "pr", "repair"}
)


def _risk_at_least(level: Any, threshold: str) -> bool:
    if threshold not in RISK_ORDER:
        raise FglError(f"unknown risk level {threshold!r}")
    return RISK_ORDER.get(level if isinstance(level, str) else "", -1) >= RISK_ORDER[threshold]


def _matches_any(paths: Any, globs: Any) -> bool:
    if not paths or not globs:
        return False
    return any(fnmatch.fnmatchcase(str(p), str(g)) for p in paths for g in globs)


def _all_match(paths: Any, globs: Any) -> bool:
    if not paths or not globs:
        return False
    return all(any(fnmatch.fnmatchcase(str(p), str(g)) for g in globs) for p in paths)


def _len(x: Any) -> int:
    return len(x) if isinstance(x, (list, tuple, dict, str)) else 0


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "risk_at_least": _risk_at_least,  # risk_at_least(risk.level, 'high')
    "matches_any": _matches_any,  # matches_any(nodes['implement'].paths, ['**/*.go'])
    "all_match": _all_match,  # all_match(nodes['implement'].paths, ['docs/**', '*.md'])
    "len": _len,
}

_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Name, ast.Load, ast.Attribute, ast.Subscript, ast.Constant, ast.List, ast.Tuple, ast.Call,
)


class FglError(ValueError):
    pass


class _Missing:
    """Null-safe sentinel: attribute/subscript on a missing value stays missing; compares as None."""


_MISSING = _Missing()


def parse(expr: str) -> ast.Expression:
    if len(expr) > MAX_CHARS:
        raise FglError("guard longer than 512 characters")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise FglError(f"syntax error: {e.msg}") from None
    count = 0
    for node in ast.walk(tree):
        count += 1
        if not isinstance(node, _ALLOWED):
            raise FglError(f"construct not allowed: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in GUARD_ROOTS and node.id not in FUNCTIONS:
            raise FglError(f"unknown name {node.id!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise FglError("only whitelisted positional function calls are allowed")
        if isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Constant):
            raise FglError("subscripts must be constants")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise FglError("private attributes are not allowed")
    if count > MAX_NODES:
        raise FglError("guard has more than 64 AST nodes")
    return tree


def _norm(v: Any) -> Any:
    return None if v is _MISSING else v


def _ev(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _ev(node.body, ctx)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_norm(_ev(e, ctx)) for e in node.elts]
    if isinstance(node, ast.Name):
        return ctx.get(node.id, _MISSING)
    if isinstance(node, ast.Attribute):
        base = _ev(node.value, ctx)
        return base.get(node.attr, _MISSING) if isinstance(base, dict) else _MISSING
    if isinstance(node, ast.Subscript):
        base = _ev(node.value, ctx)
        key = node.slice.value  # type: ignore[attr-defined]
        if isinstance(base, dict):
            return base.get(key, _MISSING)
        if isinstance(base, list) and isinstance(key, int) and -len(base) <= key < len(base):
            return base[key]
        return _MISSING
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_norm(_ev(v, ctx))) for v in node.values)
        return any(bool(_norm(_ev(v, ctx))) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return not bool(_norm(_ev(node.operand, ctx)))
    if isinstance(node, ast.Compare):
        left = _norm(_ev(node.left, ctx))
        for op, comp in zip(node.ops, node.comparators):
            right = _norm(_ev(comp, ctx))
            if not _cmp(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        fn = FUNCTIONS[node.func.id]  # type: ignore[attr-defined]
        return fn(*[_norm(_ev(a, ctx)) for a in node.args])
    raise FglError(f"unexpected node {type(node).__name__}")


def _cmp(op: ast.cmpop, a: Any, b: Any) -> bool:
    try:
        if isinstance(op, ast.Eq):
            return a == b
        if isinstance(op, ast.NotEq):
            return a != b
        if isinstance(op, ast.In):
            return a in b if b is not None else False
        if isinstance(op, ast.NotIn):
            return a not in b if b is not None else True
        if a is None or b is None:
            return False  # ordering against null is false, never an error
        if isinstance(op, ast.Lt):
            return a < b
        if isinstance(op, ast.LtE):
            return a <= b
        if isinstance(op, ast.Gt):
            return a > b
        if isinstance(op, ast.GtE):
            return a >= b
    except TypeError:
        return False
    raise FglError("unknown comparison")


class Guard:
    """A parsed guard. Parse at publish/compile time; evaluate in workflow code."""

    __slots__ = ("source", "_tree")

    def __init__(self, source: str) -> None:
        self.source = source
        self._tree = parse(source)

    def __call__(self, ctx: dict[str, Any]) -> bool:
        return bool(_norm(_ev(self._tree, ctx)))

    def __repr__(self) -> str:
        return f"Guard({self.source!r})"
