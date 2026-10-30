"""Deterministic checks of repo-scan candidates (MEM-D20; write-path.md G6). No LLM: the scanner proposes, this decides.

1. Already documented: a statement that the repo instruction files already state (keyword coverage or trigram
   similarity against each line/bullet), or that a linter/formatter config enforces (the tool is named and configured,
   a configured rule ID is named, or a formatting topic with a formatter config present), is dropped with
   `documented_in: [paths]`. Agents see the instruction files every run (repoinstr.go), and linters enforce the rest.
2. Verified citations: every proposal must cite files (`evidence.files` or `source.files`) that exist at the scanned
   commit, and supply a checkable `evidence.pattern` (a Python regex, `expect` present|absent) over files selected by
   `evidence.applies_to` (globs; default: the scope's path_globs, else the cited files' extensions). Up to
   SAMPLE_SIZE files are sampled deterministically; matched/sampled must be >= MIN_RATIO with >= MIN_SAMPLED files.
   Verified proposals carry `verification` and are labelled "Inferred convention (seen in X of Y files)".

`verify(proposals, snapshot)` is pure; `fetch_snapshot` reads what it needs through pr_inspect (Gitea at the SHA).
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md")  # = repoinstr.go
SAMPLE_SIZE = 10
MIN_SAMPLED = 3
MIN_RATIO = 0.7
MAX_FILE_BYTES = 65536
MAX_PATTERN = 200

# linter/formatter configs: basename regex -> tool names they configure
LINT_CONFIGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"^\.golangci\.(ya?ml|toml|json)$", ("golangci", "golangci-lint")),
    (r"^\.?ruff\.toml$", ("ruff",)),
    (r"^\.eslintrc(\.(js|cjs|json|ya?ml))?$|^eslint\.config\.(js|mjs|cjs|ts)$", ("eslint",)),
    (r"^\.prettierrc(\.(js|cjs|json|ya?ml|toml))?$|^prettier\.config\.(js|cjs|mjs)$", ("prettier",)),
    (r"^\.editorconfig$", ("editorconfig",)),
    (r"^\.flake8$|^\.pylintrc$|^pylintrc$", ("flake8", "pylint")),
    (r"^\.rubocop\.ya?ml$", ("rubocop",)),
    (r"^\.clang-format$", ("clang-format",)),
    (r"^\.?rustfmt\.toml$|^clippy\.toml$", ("rustfmt", "clippy")),
    (r"^\.stylelintrc(\.(js|json|ya?ml))?$", ("stylelint",)),
    (r"^biome\.jsonc?$", ("biome",)),
    (r"^\.pre-commit-config\.ya?ml$", ("pre-commit",)),
    (r"^\.markdownlint(\.(json|ya?ml))?$", ("markdownlint",)),
)
# configs that only count when they mention a tool section
SECTION_CONFIGS = {"pyproject.toml": (r"^\[tool\.(ruff|black|isort|pylint|mypy)", ("ruff", "black", "isort", "pylint")),
                   "setup.cfg": (r"^\[(flake8|isort|pylint)", ("flake8", "isort", "pylint")),
                   "package.json": (r'"(eslintConfig|prettier)"\s*:', ("eslint", "prettier"))}
CI_FILES = r"^(\.github|\.gitea)/workflows/[^/]+\.ya?ml$|^Makefile$|^justfile$|^Taskfile\.ya?ml$"
TOOLS = ("gofmt", "goimports", "golangci-lint", "golangci", "go vet", "staticcheck", "ruff", "black", "isort",
         "flake8", "pylint", "mypy", "eslint", "prettier", "editorconfig", "rubocop", "clang-format", "rustfmt",
         "clippy", "stylelint", "biome", "markdownlint")
FORMAT_TOPIC = re.compile(r"\b(indent(ation|ed)?|tabs?|spaces|whitespace|line length|line width|max(imum)? line|"
                          r"trailing (comma|newline|whitespace)|final newline|semicolons?|quote style|single quotes|"
                          r"double quotes|import (order|sorting|grouping)|sort(ed)? imports|gofmt|formatt(ed|ing))\b", re.I)
STOP = frozenset("""a an the and or but if then else of to in on for with by from as at is are be been being this that
these those it its into use uses using used never always must should do does not no any all each every per via when
which who what where than so such only also more most other same new code file files function functions""".split())


def glob_match(path: str, pat: str) -> bool:
    pat = pat.lstrip("/")
    if pat.startswith("**/") and glob_match(path, pat[3:]):
        return True
    if pat.endswith("/**"):
        return path.startswith(pat[:-3] + "/") or fnmatch.fnmatchcase(path, pat[:-3] + "/*")
    rx = re.escape(pat).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(rx, path) is not None


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", s.lower().replace("-", "_")) if w not in STOP}


def _trigrams(s: str) -> set[str]:
    s = " " + re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() + " "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def similarity(a: str, b: str) -> float:
    """pg_trgm-style trigram Jaccard similarity."""
    ta, tb = _trigrams(a), _trigrams(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _chunks(doc: str) -> list[str]:
    """Bullets/lines/sentences of an instruction file, plus adjacent-line pairs (wrapped bullets)."""
    lines = [re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", ln).strip() for ln in doc.splitlines()]
    lines = [ln for ln in lines if len(ln) > 8 and not ln.startswith("#")]
    out = lines + [f"{a} {b}" for a, b in zip(lines, lines[1:])]
    for ln in lines:
        out += [s for s in re.split(r"(?<=[.;])\s+", ln) if len(s) > 8]
    return out


def documented_by_instructions(statement: str, docs: dict[str, str], *, cover: float = 0.6, sim: float = 0.45,
                               min_shared: int = 3) -> list[str]:
    sw = _words(statement)
    hits = []
    for path, doc in docs.items():
        for ch in _chunks(doc):
            shared = sw & _words(ch)
            if (sw and len(shared) >= min(min_shared, len(sw)) and len(shared) / len(sw) >= cover) \
                    or similarity(statement, ch) >= sim:
                hits.append(path)
                break
    return hits


def lint_configs(tree: list[str]) -> dict[str, tuple[str, ...]]:
    """path -> tool names, for linter/formatter config files in the tree (any depth <= 2)."""
    out: dict[str, tuple[str, ...]] = {}
    for p in tree:
        if p.count("/") > 2:
            continue
        base = p.rsplit("/", 1)[-1]
        for rx, tools in LINT_CONFIGS:
            if re.search(rx, base):
                out[p] = tools
        if p in SECTION_CONFIGS or re.search(CI_FILES, p):
            out.setdefault(p, ())
    return out


def documented_by_linters(statement: str, configs: dict[str, tuple[str, ...]], texts: dict[str, str],
                          tree: list[str]) -> list[str]:
    s = statement.lower()
    named = [t for t in TOOLS if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", s)]
    hits: list[str] = []
    for path, tools in configs.items():
        body = texts.get(path) or ""
        if path in SECTION_CONFIGS:
            rx, sec_tools = SECTION_CONFIGS[path]
            if not re.search(rx, body, re.M):
                continue
            tools = sec_tools
        mentions = [t for t in named if t in tools or (t in body.lower() and len(t) > 3)]
        rule_ids = {w for w in re.findall(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+|[a-z]{6,}", body.lower())} - STOP
        names_rule = any(re.search(r"(?<![a-z0-9_-])" + re.escape(r) + r"(?![a-z0-9_-])", s) for r in rule_ids
                         if len(r) >= 6 and r not in {"enable", "disable", "linters", "settings", "version", "include",
                                                      "exclude", "default", "extends", "plugins", "true", "false"})
        formatter = bool(tools) and FORMAT_TOPIC.search(s) is not None and not re.search(CI_FILES, path)
        if mentions or (names_rule and tools) or formatter:
            hits.append(path)
    if not hits and re.search(r"\bgofmt|goimports|go fmt\b", s) and "go.mod" in tree:
        hits.append("go.mod")  # gofmt is the Go toolchain's canonical formatter
    return hits


def _sample(paths: list[str], n: int) -> list[str]:
    return sorted(paths, key=lambda p: hashlib.sha256(p.encode()).hexdigest())[:n]


def _safe_regex(pat: Any) -> re.Pattern[str] | None:
    """A bounded regex: <= MAX_PATTERN chars, compiles, no nested quantifiers (catastrophic backtracking)."""
    if not isinstance(pat, str) or not pat or len(pat) > MAX_PATTERN:
        return None
    if re.search(r"\([^)]*[*+][^)]*\)[*+{]", pat) or re.search(r"\\[1-9]", pat):
        return None
    try:
        return re.compile(pat, re.M)
    except re.error:
        return None


def evidence_of(p: dict[str, Any]) -> dict[str, Any]:
    ev = dict(p.get("evidence") or {})
    src = p.get("source") or {}
    ev.setdefault("files", src.get("files") or p.get("cited_files") or [])
    for k in ("pattern", "applies_to", "expect"):
        if k not in ev and k in src:
            ev[k] = src[k]
    ev["files"] = [str(f).lstrip("./") if str(f).startswith("./") else str(f) for f in ev["files"] or []
                   if isinstance(f, str)][:20]
    return ev


def candidate_files(p: dict[str, Any], ev: dict[str, Any], tree: list[str]) -> list[str]:
    """Files the pattern is checked on: evidence.applies_to globs; else the scope's path_globs; narrowed to the cited
    files' extensions when no applies_to is given."""
    globs = [g for g in ev.get("applies_to") or [] if isinstance(g, str)]
    exts: set[str] = set()
    if not globs:
        globs = [g for g in (p.get("proposed_scope") or {}).get("path_globs") or [] if isinstance(g, str)] or ["**"]
        exts = {f[f.rfind("."):] for f in ev["files"] if "." in f.rsplit("/", 1)[-1]}
    skip = re.compile(r"(^|/)(vendor|node_modules|third_party|\.git|dist|build)/")
    return [f for f in tree if not skip.search(f) and any(glob_match(f, g) for g in globs)
            and (not exts or any(f.endswith(e) for e in exts))]


@dataclass
class Snapshot:
    """What the verifier reads, at one commit: the full tree and the texts it fetched."""
    sha: str
    tree: list[str]
    texts: dict[str, str] = field(default_factory=dict)  # path -> text (missing = not fetched or binary)


def needed_paths(proposals: list[dict[str, Any]], tree: list[str]) -> set[str]:
    want = {p for p in INSTRUCTION_FILES if p in tree} | set(lint_configs(tree))
    for p in proposals:
        ev = evidence_of(p)
        want |= {f for f in ev["files"] if f in tree}
        want |= set(_sample(candidate_files(p, ev, tree), SAMPLE_SIZE))
    return want


def verify(proposals: list[dict[str, Any]], snap: Snapshot) -> tuple[list[dict], list[dict]]:
    """-> (verified proposals, dropped [{proposal, reason, documented_in?, verification?}])."""
    docs = {p: snap.texts[p] for p in INSTRUCTION_FILES if p in snap.texts}
    configs = lint_configs(snap.tree)
    tree = set(snap.tree)
    kept, dropped = [], []
    for p in proposals:
        stmt = str(p.get("statement") or "")
        ev = evidence_of(p)
        doc_in = documented_by_instructions(stmt, docs) + [
            c for c in documented_by_linters(stmt, configs, snap.texts, snap.tree)]
        doc_in = list(dict.fromkeys(doc_in))[:20]
        cited = ev["files"]
        missing = [f for f in cited if f not in tree]
        ver: dict[str, Any] = {"commit_sha": snap.sha, "cited_files": cited, "missing": missing,
                               "pattern": ev.get("pattern") if isinstance(ev.get("pattern"), str) else None,
                               "expect": "absent" if ev.get("expect") == "absent" else "present",
                               "matched": 0, "sampled": 0}
        rx = _safe_regex(ev.get("pattern"))
        if rx is not None:
            sample = [f for f in _sample(candidate_files(p, ev, snap.tree), SAMPLE_SIZE) if f in snap.texts]
            hit = sum(1 for f in sample if bool(rx.search(snap.texts[f][:MAX_FILE_BYTES])) == (ver["expect"] == "present"))
            ver.update(matched=hit, sampled=len(sample))
        if doc_in:
            dropped.append({"proposal": p, "reason": "already documented in " + ", ".join(doc_in[:3]),
                            "documented_in": doc_in, "verification": ver})
            continue
        why = ("no cited files" if not cited else f"cited files missing at {snap.sha[:12]}: {', '.join(missing[:3])}"
               if missing else "no checkable pattern" if rx is None
               else f"too few files to check ({ver['sampled']})" if ver["sampled"] < MIN_SAMPLED
               else f"pattern holds in {ver['matched']} of {ver['sampled']} files" if
               ver["matched"] / ver["sampled"] < MIN_RATIO else None)
        if why:
            dropped.append({"proposal": p, "reason": "unverified: " + why, "verification": ver})
            continue
        label = f"Inferred convention (seen in {ver['matched']} of {ver['sampled']} files)"
        q = {k: v for k, v in p.items() if k != "evidence"}
        q["verification"] = {k: ver[k] for k in ("cited_files", "missing", "pattern", "expect", "matched", "sampled",
                                                  "commit_sha")}
        q["documented_in"] = []
        q["label"] = label
        q["rationale"] = (label + ". " + str(p.get("rationale") or "")).strip()[:1000]
        kept.append(q)
    return kept, dropped


async def fetch_snapshot(g: Any, sha: str, proposals: list[dict[str, Any]], *, concurrency: int = 8) -> Snapshot:
    """Tree at `sha` plus the texts verify() needs, read through pr_inspect.Gitea."""
    tree = await g.tree(sha)
    snap = Snapshot(sha, tree)
    sem = asyncio.Semaphore(concurrency)

    async def one(path: str) -> None:
        async with sem:
            t = await g.file_at(sha, path, max_bytes=MAX_FILE_BYTES)
        if t is not None:
            snap.texts[path] = t
    await asyncio.gather(*(one(p) for p in sorted(needed_paths(proposals, tree))[:200]))
    return snap
