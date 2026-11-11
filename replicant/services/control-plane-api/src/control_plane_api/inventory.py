"""Repo inventory: environment inference, the fleet classifier and the impact score (design §8c; contract
docs/design/onboarding-interfaces.md §2, §3, §10). Pure functions only; persistence is in routes_inventory.py.

* `infer_environment(profile)` -> §2 `{environment, version, commands, source, supported, unsupported_reason}`.
  Sources by priority: CI workflow commands and setup actions, then devcontainer, then version files, then
  manifests and lockfiles, then the defaults per package manager.
* `classify_mcp`, `classify_hook`, `classify_toolchain`, `probe_item` -> catalogue items (kind, identity, class,
  default action); `catalog_items(profile, env)` lists every item of a profile.
* `impact(...)` turns per-item outcome aggregates into ratios and a score (§10: each delta is the rate with the item
  divided by the rate without it; 1 = no difference, None = not enough data).
"""
from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
from typing import Any

ENVIRONMENTS = {"go": "go-service", "python": "python-service", "node": "node-service"}
# What the environment images provide (images/env-*-service). Go builds older `go` directives with the newer
# toolchain; Node 18/20 projects run on Node 22.
PYTHON_VERSIONS = ("3.12", "3.13")
GO_MAX = (1, 26)
NODE_VERSION = "22"
NODE_COMPATIBLE = (18, 20, 22)
DEFAULT_VERSION = {"python": "3.12", "go": "1.26", "node": NODE_VERSION}
PACKAGE_MANAGERS = {"python": ("uv", "pip"), "node": ("npm", "pnpm", "yarn"), "go": ("go",)}
STAGES = ("install", "build", "test", "lint")

CLASSES = {
    "mcp_server": ("auto_allow", "map", "needs_egress", "needs_credentials", "review"),
    "hook": ("ok", "at_risk"),
    "toolchain": ("supported", "unsupported"),
    "probe_failure": ("missing_tool", "network", "timeout", "test_failure", "build_failure", "install_failure",
                      "lint_failure"),
}
DEFAULT_ACTION = {"auto_allow": "allow", "map": "map", "needs_egress": "skip", "needs_credentials": "skip",
                  "review": "skip", "ok": "allow", "at_risk": "warn", "supported": "allow", "unsupported": "block"}
DECISION_ACTION = {"approve": "allow", "deny": "skip"}


def digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ environment
def source_rank(source: str | None) -> int:
    s = (source or "").split("#", 1)[0]
    if s.startswith(".github/workflows/") or s == ".gitlab-ci.yml":
        return 0
    if s.startswith(".devcontainer"):
        return 1
    if s in (".tool-versions", ".python-version", ".nvmrc"):
        return 2
    return 3


_PY = re.compile(r"\b(uv|uvx|pytest|python3?|pip3?|poetry|ruff|mypy|tox|nox|black|flake8|pylint|pyright)\b")
_NODE = re.compile(r"\b(npm|npx|pnpm|yarn|node|vitest|jest|eslint|prettier|tsc|bun|bunx)\b")
_GO = re.compile(r"\b(go|gofmt|golangci-lint|staticcheck)\b")


def command_ecosystem(cmd: str) -> str | None:
    c = re.sub(r"^cd\s+\S+\s+&&\s+", "", cmd.strip())
    first = c.split()[0] if c.split() else ""
    for eco, rx in (("go", _GO), ("python", _PY), ("node", _NODE)):
        if rx.fullmatch(posixpath.basename(first)):
            return eco
    for eco, rx in (("python", _PY), ("node", _NODE), ("go", _GO)):
        if rx.search(c):
            return eco
    return None


def command_pm(cmd: str, eco: str) -> str | None:
    if eco == "python":
        if re.search(r"\buvx?\b", cmd):
            return "uv"
        if re.search(r"\bpoetry\b", cmd):
            return "poetry"
        if re.search(r"\bpip3?\b", cmd):
            return "pip"
    if eco == "node":
        m = re.search(r"\b(pnpm|yarn|npm)\b", cmd)
        return m.group(1) if m else None
    if eco == "go":
        return "go"
    return None


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _satisfies(v: tuple[int, ...], spec: str) -> bool:
    """`>=3.11,<3.14`, `~=3.12`, `^3.11`, `==3.12.*`, `3.12`, `>= 20` against a version tuple."""
    spec = re.sub(r"([<>=~^!]+)\s+", r"\1", spec.strip())
    for clause in re.split(r"[,\s]+", spec):
        if not clause:
            continue
        m = re.match(r"^(>=|<=|==|!=|~=|>|<|\^|~)?v?(\d+(?:\.\d+)*)(\.\*)?$", clause)
        if not m:
            return False
        op, want = m.group(1) or "==", _ver_tuple(m.group(2))
        n = len(want)
        cur = (v + (0, 0, 0))[:n]
        ok = {"==": cur == want, "!=": cur != want, ">=": cur >= want, ">": cur > want, "<=": cur <= want,
              "<": cur < want, "~": cur[:2] == want[:2],
              "~=": cur >= want and cur[:max(1, n - 1)] == want[:max(1, n - 1)],
              "^": cur >= want and cur[:1] == want[:1]}[op]
        if not ok:
            return False
    return True


def resolve_version(eco: str, requested: str | None) -> tuple[str | None, str | None]:
    """(environment version, unsupported reason) for an ecosystem's requested version or constraint."""
    if not requested or requested in ("latest", "lts", "stable", "system"):
        return DEFAULT_VERSION[eco], None
    req = requested.strip().lstrip("v")
    exact = re.fullmatch(r"\d+(?:\.\d+){0,2}", req)
    if eco == "python":
        if exact:
            mm = ".".join(req.split(".")[:2])
            return (mm, None) if mm in PYTHON_VERSIONS else \
                (None, f"python {req} is not available (have {', '.join(PYTHON_VERSIONS)})")
        ok = [v for v in PYTHON_VERSIONS if _satisfies(_ver_tuple(v), req)]
        return (ok[-1], None) if ok else (None, f"python {req} is not available (have {', '.join(PYTHON_VERSIONS)})")
    if eco == "node":
        if exact:
            major = _ver_tuple(req)[0]
            return (NODE_VERSION, None) if major in NODE_COMPATIBLE else \
                (None, f"node {req} is not available (have {NODE_VERSION})")
        return (NODE_VERSION, None) if _satisfies((int(NODE_VERSION), 0, 0), req) else \
            (None, f"node {req} is not available (have {NODE_VERSION})")
    # go: the toolchain builds any older `go` directive
    t = _ver_tuple(req)
    if exact:
        return (".".join(str(x) for x in t[:2]), None) if t[:2] <= GO_MAX else \
            (None, f"go {req} is newer than the toolchain ({GO_MAX[0]}.{GO_MAX[1]})")
    return (f"{GO_MAX[0]}.{GO_MAX[1]}", None) if _satisfies(GO_MAX, req) else \
        (None, f"go {req} is not available (have {GO_MAX[0]}.{GO_MAX[1]})")


def _default_commands(eco: str, pm: str, files: set[str], scripts: list[str] | None) -> dict[str, str | None]:
    if eco == "go":
        return {"install": "go mod download", "build": "go build ./...", "test": "go test -count=1 ./...",
                "lint": None}
    if eco == "python":
        if pm == "uv":
            return {"install": "uv sync --frozen", "build": None, "test": "uv run pytest -q", "lint": None}
        req = "requirements.txt" if "requirements.txt" in files else None
        return {"install": f"pip install -r {req}" if req else "pip install -e .", "build": None,
                "test": "python -m pytest -q", "lint": None}
    install = "npm ci" if pm == "npm" else f"{pm} install --frozen-lockfile"
    sc = scripts if scripts is not None else ["test"]
    return {"install": install, "build": f"{pm} run build" if "build" in sc else None,
            "test": f"{pm} test" if "test" in sc else None, "lint": f"{pm} run lint" if "lint" in sc else None}


def infer_environment(profile: dict[str, Any]) -> dict[str, Any]:
    """Contract §2 for a contract §1 profile."""
    ecos = sorted((e for e in profile.get("ecosystems") or [] if e.get("ecosystem") in ENVIRONMENTS),
                  key=lambda e: source_rank(e.get("source")))
    ci = [c for c in profile.get("ci_commands") or [] if c.get("stage") in STAGES and c.get("command")]
    files = {f.get("path") for f in profile.get("files") or []}
    primary = None
    for c in ci:                         # the ecosystem CI tests with decides
        if c["stage"] == "test" and command_ecosystem(c["command"]):
            primary = command_ecosystem(c["command"])
            break
    if primary is None and ecos:
        best = source_rank(ecos[0].get("source"))
        tied = {e["ecosystem"] for e in ecos if source_rank(e.get("source")) == best}
        primary = next(x for x in ("go", "python", "node") if x in tied)
    if primary is None:
        return {"environment": None, "version": None, "commands": dict.fromkeys(STAGES), "source": {},
                "supported": False, "unsupported_reason": "no supported ecosystem (go, python, node) detected"}
    mine = [e for e in ecos if e["ecosystem"] == primary]
    requested = next((e.get("version") for e in mine if e.get("version")), None)
    version, why = resolve_version(primary, requested)
    pm = next((command_pm(c["command"], primary) for c in ci
               if command_ecosystem(c["command"]) == primary and command_pm(c["command"], primary)), None)
    pm = pm or next((e.get("package_manager") for e in mine if e.get("package_manager")), None) or \
        PACKAGE_MANAGERS[primary][0 if primary != "python" else 1]
    if why is None and pm not in PACKAGE_MANAGERS[primary]:
        why = f"package manager {pm} is not supported ({', '.join(PACKAGE_MANAGERS[primary])})"
    scripts = next((e.get("scripts") for e in mine if e.get("scripts") is not None), None)
    commands: dict[str, str | None] = dict.fromkeys(STAGES)
    source: dict[str, str] = {}
    for stage in STAGES:
        picked = [c for c in ci if c["stage"] == stage and command_ecosystem(c["command"]) in (primary, None)]
        if picked:
            first_src = picked[0]["source"]
            same = [c["command"] for c in picked if c["source"] == first_src]
            commands[stage] = " && ".join(same)[:1000]
            source[stage] = first_src
    defaults = _default_commands(primary, pm, files, scripts)
    for stage in STAGES:
        if commands[stage] is None and defaults.get(stage):
            commands[stage] = defaults[stage]
            source[stage] = f"default:{pm}"
    return {"environment": ENVIRONMENTS[primary], "version": version, "commands": commands, "source": source,
            "supported": why is None, "unsupported_reason": why, "ecosystem": primary, "package_manager": pm,
            "requested_version": requested}


def env_digest(env: dict[str, Any]) -> str:
    """What a readiness probe depends on: environment, version and commands."""
    return digest({"environment": env.get("environment"), "version": env.get("version"),
                   "commands": env.get("commands")})


# ------------------------------------------------------------------------------------------------ classifier
SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSW(OR)?D|(^|_)PASS($|_)|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|AUTH|CREDENTIAL|"
                        r"COOKIE|SESSION|(^|_)PAT($|_)|DSN|CONNECTION_STRING|DATABASE_URL|CLIENT_SECRET)", re.I)
URLISH_KEY = re.compile(r"(_URL|_URI|_HOST|_ENDPOINT|_SERVER|BASE_URL)$", re.I)
EXACT_NPM = re.compile(r"^(@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*@\d+\.\d+\.\d+(-[\w.]+)?$", re.I)
EXACT_PY = re.compile(r"^[a-z0-9][\w.-]*(==|@)\d+(\.\d+){1,2}([a-z0-9.+-]*)$", re.I)
PKG_RUNNERS = {"npx", "uvx", "pipx", "bunx", "pnpm", "yarn"}
# platform services that replace a repo MCP server (design §8c "Has a platform equivalent")
PLATFORM_EQUIVALENTS = (("server-memory", "memory"), ("mcp-memory", "memory"), ("context7", "docs"),
                        ("sourcegraph", "code_search"), ("server-filesystem", "workspace"),
                        ("mcp-server-git", "workspace"), ("server-git", "workspace"))


def mcp_identity(server: dict[str, Any]) -> str:
    return str(server.get("identity") or "")[:400]


def classify_mcp(server: dict[str, Any]) -> tuple[str, str]:
    """(class, reason) of one profile MCP server (contract §3)."""
    ident = mcp_identity(server)
    for needle, svc in PLATFORM_EQUIVALENTS:
        if needle in ident:
            return "map", f"platform equivalent: {svc}"
    env_keys = [str(k) for k in server.get("env_keys") or []]
    header_keys = [str(k) for k in server.get("header_keys") or []]
    secret = [k for k in env_keys + header_keys if SECRET_KEY.search(k)]
    if secret:
        return "needs_credentials", f"credentials in {', '.join(sorted(secret)[:5])}"
    if server.get("url") or server.get("transport") not in ("stdio", None):
        return "needs_egress", f"connects to {server.get('url') or ident}"
    urlish = [k for k in env_keys if URLISH_KEY.search(k)]
    if urlish:
        return "needs_egress", f"network target in {', '.join(sorted(urlish)[:5])}"
    cmd = posixpath.basename(str(server.get("command") or ""))
    pkg = ident.split(":", 2)[2] if ident.count(":") >= 2 else ""
    if cmd in PKG_RUNNERS and (EXACT_NPM.match(pkg) or EXACT_PY.match(pkg)):
        return "auto_allow", f"local stdio, pinned {pkg}"
    if cmd in PKG_RUNNERS:
        return "review", f"package {pkg or '?'} is not pinned to an exact version"
    return "review", f"unrecognised stdio command {cmd or '?'}"


NETWORK = re.compile(r"(\b(curl|wget|ssh|scp|rsync|nc|telnet|gh|aws|gcloud|az|kubectl|docker|podman|uvx|pipx)\b|"
                     r"\bnpx\s+(-y|--yes)\b|\bnpm\s+(install|i|ci)\b|\bpnpm\s+(install|add|dlx)\b|"
                     r"\byarn\s+(add|dlx)\b|\bpip3?\s+install\b|\bgo\s+(get|install)\b|"
                     r"\bgit\s+(push|pull|fetch|clone|ls-remote)\b|https?://)")
COMMON_TOOLS = {"sh", "bash", "env", "true", "false", "test", "[", "echo", "printf", "cat", "head", "tail", "grep",
                "egrep", "sed", "awk", "jq", "xargs", "find", "sort", "uniq", "wc", "tr", "cut", "tee", "mkdir", "rm",
                "cp", "mv", "touch", "chmod", "date", "basename", "dirname", "git", "make", "diff", "patch", "cd",
                "exit", "read", "set", "export", "if", "then", "else", "elif", "fi", "for", "do", "done", "while",
                "case", "esac", "stat", "realpath", "readlink", "sleep", "timeout", "command", "which", "type", "exec",
                "source", ".", "zstd", "tar", "gzip", "ls", "pwd", "local", "return", "shift", "trap", "wait"}
ENV_TOOLS = {"go-service": {"go", "gofmt"}, "python-service": {"python", "python3", "uv", "pip", "pip3"},
             "node-service": {"node", "npm", "npx", "pnpm", "yarn", "corepack"}}


def _segments(cmd: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|\|\||;|\||\n", cmd) if s.strip()]


def hook_binaries(cmd: str) -> list[str]:
    out = []
    for seg in _segments(cmd):
        words = [w for w in seg.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        while words and words[0] in ("!", "(", "{", "then", "do", "else", "time", "if", "while"):
            words = words[1:]
        if not words:
            continue
        w = words[0].strip("\"'(){}")
        if not w or w.startswith(("$", "./", "../")) or w.startswith(".claude/") or \
                ("/" in w and not w.startswith("/")):
            continue                                        # repo scripts: present in the checkout
        out.append(posixpath.basename(w))
    return out


def classify_hook(hook: dict[str, Any], environment: str | None) -> tuple[str, str]:
    cmd = str(hook.get("command") or "")
    m = NETWORK.search(cmd)
    if m:
        return "at_risk", f"network: {m.group(0).strip()[:40]}"
    have = COMMON_TOOLS | ENV_TOOLS.get(environment or "", set())
    missing = [b for b in hook_binaries(cmd) if b not in have]
    if missing:
        return "at_risk", f"binary not in {environment or 'the environment'}: {missing[0][:40]}"
    return "ok", "runs offline with the environment's tools"


def hook_identity(hook: dict[str, Any]) -> str:
    return f"hook:{hook.get('event')}:{hook.get('command_hash')}"


def toolchain_item(env: dict[str, Any]) -> dict[str, Any] | None:
    eco = env.get("ecosystem")
    if not eco:
        return None
    ver = env.get("version") if env.get("supported") else (env.get("requested_version") or env.get("version"))
    cls = "supported" if env.get("supported") else "unsupported"
    return {"kind": "toolchain", "identity": f"toolchain:{eco}:{ver or 'any'}:{env.get('package_manager')}"[:400],
            "class": cls, "default_action": DEFAULT_ACTION[cls],
            "details": {"environment": env.get("environment"), "reason": env.get("unsupported_reason")}}


def probe_item(environment: str | None, cause: str, detail: str | None) -> dict[str, Any]:
    cause = cause if cause in CLASSES["probe_failure"] else "build_failure"
    det = re.sub(r"[^\w.+@/-]", "_", (detail or "").strip())[:120] or "-"
    return {"kind": "probe_failure", "identity": f"probe:{environment or 'none'}:{cause}:{det}", "class": cause,
            "default_action": "warn", "details": {"environment": environment, "cause": cause, "detail": detail}}


def catalog_items(profile: dict[str, Any], env: dict[str, Any]) -> list[dict[str, Any]]:
    """Every catalogue item of a profile (deduplicated by kind and identity)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for s in profile.get("mcp_servers") or []:
        cls, why = classify_mcp(s)
        ident = mcp_identity(s)
        if ident:
            out[("mcp_server", ident)] = {"kind": "mcp_server", "identity": ident, "class": cls,
                                          "default_action": DEFAULT_ACTION[cls],
                                          "details": {"reason": why, "transport": s.get("transport"),
                                                      "name": s.get("name"), "command": s.get("command"),
                                                      "args": (s.get("args") or [])[:10]}}
    for h in (profile.get("claude") or {}).get("hooks") or []:
        cls, why = classify_hook(h, env.get("environment"))
        out[("hook", hook_identity(h))] = {"kind": "hook", "identity": hook_identity(h), "class": cls,
                                           "default_action": DEFAULT_ACTION[cls],
                                           "details": {"reason": why, "event": h.get("event"),
                                                       "matcher": h.get("matcher"),
                                                       "command": str(h.get("command") or "")[:300]}}
    t = toolchain_item(env)
    if t:
        out[("toolchain", t["identity"])] = t
    return list(out.values())


def effective_action(default_action: str, decision: str | None, map_to: str | None = None) -> str:
    if decision == "map":
        return f"map:{map_to}" if map_to else "map"
    return DECISION_ACTION.get(decision or "", default_action)


def warning_message(kind: str, cls: str, details: dict[str, Any]) -> str:
    reason = (details or {}).get("reason") or ""
    return {"mcp_server": f"MCP server skipped ({cls.replace('_', ' ')}){': ' + reason if reason else ''}",
            "hook": f"hook may fail in the sandbox: {reason}",
            "toolchain": f"toolchain not supported: {reason}",
            "probe_failure": f"readiness probe: {cls.replace('_', ' ')}"
            + (f" ({details.get('detail')})" if (details or {}).get("detail") else "")}.get(kind, reason)


# ------------------------------------------------------------------------------------------------ impact
METRICS = ("qa_first_pass", "rejected", "repair_cycles", "failed")


def _ratio(a_x: float, a_n: int, b_x: float, b_n: int) -> float | None:
    """(rate with) / (rate without), add-half smoothed so an empty numerator or denominator stays finite; None when
    either side has no jobs."""
    if a_n <= 0 or b_n <= 0:
        return None
    return round(((a_x + 0.5) / (a_n + 1)) / ((b_x + 0.5) / (b_n + 1)), 3)


def impact(agg: dict[str, Any], kind: str = "", repos: int = 0) -> dict[str, Any]:
    """agg: jobs_with/jobs_without and, per side, failed, rejected, repair_cycles (sums), qa_jobs and qa_first_pass
    (counts) -> {score, jobs_with, jobs_without, delta}. Higher scores hurt more outcomes in more jobs. Delta ratios:
    failed, rejected and repair_cycles above 1 are worse; qa_first_pass below 1 is worse."""
    w, wo = int(agg.get("jobs_with") or 0), int(agg.get("jobs_without") or 0)
    delta = {
        "qa_first_pass": _ratio(agg.get("w_qa_first_pass") or 0, int(agg.get("w_qa_jobs") or 0),
                                agg.get("wo_qa_first_pass") or 0, int(agg.get("wo_qa_jobs") or 0)),
        "rejected": _ratio(agg.get("w_rejected") or 0, w, agg.get("wo_rejected") or 0, wo),
        "repair_cycles": _ratio(agg.get("w_repair_cycles") or 0, w, agg.get("wo_repair_cycles") or 0, wo),
        "failed": _ratio(agg.get("w_failed") or 0, w, agg.get("wo_failed") or 0, wo),
    }
    logs = [math.log(v) for k, v in delta.items() if v and k != "qa_first_pass"]
    if delta["qa_first_pass"]:
        logs.append(-math.log(delta["qa_first_pass"]))
    if logs:
        score = w * (sum(logs) / len(logs))
    elif w:      # no comparable jobs without the item: the item's own bad-outcome rate
        score = w * ((agg.get("w_failed") or 0) + (agg.get("w_rejected") or 0)) / w
    else:
        score = 0.0
    if kind == "probe_failure":
        score += repos                         # a blocked repo takes no jobs at all
    return {"score": round(score, 3), "jobs_with": w, "jobs_without": wo, "delta": delta}
