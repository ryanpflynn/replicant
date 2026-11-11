"""Repo inventory scanner (design §8c, plan Phase 1.2, contract ONBOARD-01 §1).

Reads a repository's configuration at the default-branch head through the SCM API (never a clone), builds the repo
profile and sends it to control-plane-api `PUT /internal/v1/repos/{repo_id}/profile`, which classifies it, infers the
environment and owns the `inventory` schema. Only names, hashes, parsed commands and env/header KEYS leave this
module: file contents and env values never do.

When it runs (`Scanner.request`): repo registration (all `register_repo` call sites), a default-branch change, a push
to the default branch that touches a watched path (or a repo this process has not profiled yet), and the admin
"rescan" (`POST /internal/v1/inventory:rescan`). Requests are coalesced per repo and processed by `Scanner.loop`;
every failure is logged and never affects webhook processing.

The parsers below are pure functions (unit-tested without Gitea).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import posixpath
import re
import tomllib
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import yaml

log = logging.getLogger(__name__)

SCANNER_VERSION = 1
MAX_READ_BYTES = 262144
MAX_CLAUDE_MD = 50
MAX_WORKFLOWS = 30

# Root files read for their content (version files, manifests, CI, agent config).
ROOT_READ = {
    "AGENTS.md", "CLAUDE.md", ".mcp.json", ".gitlab-ci.yml", ".tool-versions", ".python-version", ".nvmrc", "go.mod",
    "pyproject.toml", "package.json", ".pre-commit-config.yaml", ".claude/settings.json", ".claude/CLAUDE.md",
    ".devcontainer/devcontainer.json", ".devcontainer.json", ".github/copilot-instructions.md",
}
# Root files whose presence matters (lockfiles: never read, only listed with their size).
ROOT_PRESENT = {"uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
                "yarn.lock", "go.sum", "Makefile"}
REQUIREMENTS_RE = re.compile(r"^requirements[\w.-]*\.txt$")
WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
CLAUDE_DIRS = ("rules", "skills", "commands", "agents")


def watched(path: str) -> bool:
    """A path whose change can change the profile (push trigger)."""
    return (path in ROOT_READ or path in ROOT_PRESENT or bool(REQUIREMENTS_RE.match(path))
            or bool(WORKFLOW_RE.match(path)) or posixpath.basename(path) == "CLAUDE.md"
            or path.startswith((".claude/", ".devcontainer/")))


def push_touches_watched(payload: dict[str, Any]) -> bool:
    """True when a push payload's commits touch a watched path, or the payload cannot tell (no or truncated commit
    lists: Gitea sends at most 20 commits with file lists)."""
    commits = payload.get("commits") or []
    total = payload.get("total_commits")
    if not commits or (isinstance(total, int) and total > len(commits)):
        return True
    for c in commits:
        for key in ("added", "modified", "removed"):
            if any(watched(p) for p in c.get(key) or [] if isinstance(p, str)):
                return True
    return False


def _sha256(text_: str) -> str:
    return "sha256:" + hashlib.sha256(text_.encode("utf-8")).hexdigest()


def to_read(paths: list[str]) -> list[str]:
    """The files whose content the profile needs, in a stable order (bounded)."""
    out = [p for p in paths if p in ROOT_READ]
    out += sorted(p for p in paths if WORKFLOW_RE.match(p))[:MAX_WORKFLOWS]
    nested = sorted(p for p in paths if posixpath.basename(p) == "CLAUDE.md" and p not in ROOT_READ)
    out += nested[:MAX_CLAUDE_MD]
    return sorted(set(out))


# ------------------------------------------------------------------------------------------------ CI commands
DROP_TOOLS = {"docker", "docker-compose", "podman", "kubectl", "helm", "aws", "gcloud", "az", "gh", "twine", "git",
              "curl", "wget", "echo", "cat", "ls", "mkdir", "cp", "mv", "rm", "export", "sudo", "apt-get", "apt", "apk",
              "brew", "choco", "ssh", "scp", "rsync", "terraform", "ansible", "sleep", "set", "source", "corepack",
              "printf", "chmod", "touch", "tee", "if", "then", "else", "fi", "for", "do", "done", "while", "case",
              "esac", "exit", "cd", "pushd", "popd", "codecov", "sonar-scanner", "npx-semantic-release"}
DROP_WORDS = re.compile(r"\b(deploy|release|publish|upload|codecov|coveralls|sonar|semantic-release)\b", re.I)
_PM = r"(?:npm|pnpm|yarn|bun)"
STAGES: list[tuple[str, re.Pattern[str]]] = [
    ("lint", re.compile(rf"\b(ruff|flake8|pylint|mypy|pyright|black\s+--check|isort\s+--check|eslint|prettier|"
                        rf"golangci-lint|go\s+vet|gofmt|staticcheck|pre-commit\s+run|tsc\s+--noEmit|"
                        rf"{_PM}\s+(run\s+)?(lint|typecheck|format:check|check)\b|make\s+(lint|check)\b)")),
    ("test", re.compile(rf"\b(pytest|go\s+test|vitest|jest|mocha|tox|nox|unittest|"
                        rf"{_PM}\s+(run\s+)?test\b|make\s+test\b)")),
    ("build", re.compile(rf"\b(go\s+build|{_PM}\s+(run\s+)?build\b|tsc\b|uv\s+build|python3?\s+-m\s+build|"
                         rf"make\s+build\b)")),
    ("install", re.compile(r"\b(uv\s+sync|uv\s+pip\s+install|pip3?\s+install|python3?\s+-m\s+pip\s+install|"
                           r"poetry\s+install|npm\s+(ci|install|i)\b|(pnpm|yarn|bun)\s+install\b|^yarn$|"
                           r"go\s+mod\s+download)")),
]
_SETUP = {"actions/setup-python": ("python", "python-version"), "actions/setup-node": ("node", "node-version"),
          "actions/setup-go": ("go", "go-version")}


def _first_word(cmd: str) -> str:
    for w in cmd.split():
        if "=" in w and not w.startswith(("-", "=")) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            continue                                   # FOO=bar cmd
        return posixpath.basename(w)
    return ""


def classify_ci_line(line: str) -> str | None:
    """The stage of one CI shell command, or None when it is not a build/test/lint/install step."""
    cmd = line.strip()
    if not cmd or cmd.startswith("#") or "${{" in cmd or DROP_WORDS.search(cmd):
        return None
    if _first_word(cmd) in DROP_TOOLS:
        return None
    for stage, rx in STAGES:
        if rx.search(cmd):
            return stage
    return None


def shell_lines(run: str) -> list[str]:
    """Logical lines of a `run:`/`script:` block (backslash continuations joined)."""
    out: list[str] = []
    cur = ""
    for raw in str(run).splitlines():
        ln = raw.rstrip()
        if ln.endswith("\\"):
            cur += ln[:-1].strip() + " "
            continue
        cur += ln.strip()
        if cur.strip():
            out.append(cur.strip())
        cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def _commands_of(lines: list[str], source: str, workdir: str | None) -> list[dict[str, Any]]:
    out = []
    for ln in lines:
        stage = classify_ci_line(ln)
        if stage is None:
            continue
        cmd = ln if not workdir or workdir in (".", "./") else f"cd {workdir} && {ln}"
        out.append({"stage": stage, "command": cmd[:500], "source": source})
    return out


def _version_str(v: Any) -> str | None:
    if v is None or isinstance(v, (list, dict, bool)):
        return None
    s = str(v).strip().lstrip("v")
    return s if s and "${{" not in s and len(s) <= 40 else None


def parse_github_workflow(path: str, text_: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(ci_commands, ecosystems) of one GitHub Actions workflow: `run:` steps of every job that is not a deploy or
    release job, and the toolchain versions of setup-* actions."""
    doc = yaml.safe_load(text_)
    if not isinstance(doc, dict):
        raise ValueError("not a workflow mapping")
    cmds: list[dict[str, Any]] = []
    ecos: list[dict[str, Any]] = []
    default_wd = ((doc.get("defaults") or {}).get("run") or {}).get("working-directory")
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict) or DROP_WORDS.search(f"{job_id} {job.get('name') or ''}"):
            continue
        source = f"{path}#jobs.{job_id}"
        job_wd = ((job.get("defaults") or {}).get("run") or {}).get("working-directory") or default_wd
        pm_hint: dict[str, str] = {}
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            if uses:
                action = uses.split("@", 1)[0]
                w = step.get("with") or {}
                if action in _SETUP:
                    eco, key = _SETUP[action]
                    ver = _version_str(w.get(key))
                    entry: dict[str, Any] = {"ecosystem": eco, "version": ver, "package_manager": None,
                                             "source": source}
                    if eco == "node" and w.get("cache") in ("npm", "pnpm", "yarn"):
                        entry["package_manager"] = w["cache"]
                    ecos.append(entry)
                elif action == "astral-sh/setup-uv":
                    pm_hint["python"] = "uv"
                elif action == "pnpm/action-setup":
                    pm_hint["node"] = "pnpm"
                continue
            if step.get("run"):
                cmds += _commands_of(shell_lines(step["run"]), source, step.get("working-directory") or job_wd)
        for e in ecos:
            if e["source"] == source and e["package_manager"] is None:
                e["package_manager"] = pm_hint.get(e["ecosystem"])
        for eco, pm in pm_hint.items():
            if not any(e["ecosystem"] == eco and e["source"] == source for e in ecos):
                ecos.append({"ecosystem": eco, "version": None, "package_manager": pm, "source": source})
    return cmds, ecos


GITLAB_RESERVED = {"stages", "variables", "default", "include", "image", "services", "workflow", "before_script",
                   "after_script", "cache", "pages"}
IMAGE_RE = re.compile(r"(?:^|/)(python|node|golang|go)(?::|-)v?(\d+(?:\.\d+){0,2})")


def ecosystem_of_image(image: str | None, source: str) -> dict[str, Any] | None:
    """`python:3.12-slim`, `mcr.microsoft.com/devcontainers/python:1-3.12`, `node:22`, `golang:1.26` -> an
    ecosystem entry."""
    if not image:
        return None
    m = IMAGE_RE.search(str(image).replace("devcontainers/python:1-", "devcontainers/python:")
                        .replace("devcontainers/typescript-node:1-", "devcontainers/node:")
                        .replace("devcontainers/javascript-node:1-", "devcontainers/node:")
                        .replace("devcontainers/go:1-", "devcontainers/go:"))
    if not m:
        return None
    eco = {"golang": "go"}.get(m.group(1), m.group(1))
    return {"ecosystem": eco, "version": m.group(2), "package_manager": None, "source": source}


def parse_gitlab_ci(text_: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    doc = yaml.safe_load(text_)
    if not isinstance(doc, dict):
        raise ValueError("not a mapping")
    cmds: list[dict[str, Any]] = []
    ecos: list[dict[str, Any]] = []
    top_image = doc.get("image") if isinstance(doc.get("image"), str) else (doc.get("image") or {}).get("name") \
        if isinstance(doc.get("image"), dict) else None
    for job_id, job in doc.items():
        if job_id in GITLAB_RESERVED or str(job_id).startswith(".") or not isinstance(job, dict):
            continue
        if DROP_WORDS.search(f"{job_id} {job.get('stage') or ''}"):
            continue
        source = f".gitlab-ci.yml#{job_id}"
        lines: list[str] = []
        for key in ("before_script", "script"):
            v = job.get(key)
            for item in ([v] if isinstance(v, str) else v or []):
                lines += shell_lines(str(item))
        cmds += _commands_of(lines, source, None)
        img = job.get("image")
        img = img.get("name") if isinstance(img, dict) else img
        e = ecosystem_of_image(img or top_image, source)
        if e:
            ecos.append(e)
    return cmds, ecos


# ------------------------------------------------------------------------------------------------ toolchain files
def parse_tool_versions(text_: str) -> list[dict[str, Any]]:
    names = {"python": "python", "nodejs": "node", "node": "node", "golang": "go", "go": "go"}
    out = []
    for ln in text_.splitlines():
        parts = ln.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] in names:
            out.append({"ecosystem": names[parts[0]], "version": _version_str(parts[1]), "package_manager": None,
                        "source": ".tool-versions"})
    return out


def parse_go_mod(text_: str) -> dict[str, Any]:
    ver = None
    for ln in text_.splitlines():
        m = re.match(r"^\s*go\s+(\d+\.\d+(?:\.\d+)?)\s*$", ln)
        if m:
            ver = m.group(1)
    return {"ecosystem": "go", "version": ver, "package_manager": "go", "source": "go.mod"}


def _strip_jsonc(text_: str) -> str:
    """JSON with comments and trailing commas (devcontainer.json, sometimes settings.json) -> JSON."""
    out, i, n, in_str = [], 0, len(text_), False
    while i < n:
        c = text_[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text_[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text_.startswith("//", i):
            j = text_.find("\n", i)
            i = n if j < 0 else j
        elif text_.startswith("/*", i):
            j = text_.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_jsonc(text_: str) -> Any:
    try:
        return json.loads(text_)
    except ValueError:
        return json.loads(_strip_jsonc(text_))


def parse_devcontainer(text_: str, source: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doc = load_jsonc(text_)
    if not isinstance(doc, dict):
        raise ValueError("not an object")
    feats = doc.get("features") or {}
    dc: dict[str, Any] = {"image": doc.get("image") if isinstance(doc.get("image"), str) else None,
                          "features": sorted(str(k) for k in feats) if isinstance(feats, dict) else []}
    build = doc.get("build") or {}
    if isinstance(build, dict) and build.get("dockerfile"):
        dc["dockerfile"] = str(build["dockerfile"])
    ecos = []
    e = ecosystem_of_image(dc["image"], source)
    if e:
        ecos.append(e)
    if isinstance(feats, dict):
        for k, opts in feats.items():
            m = re.search(r"/features/(python|node|go)(?::|$)", str(k))
            if m:
                ver = _version_str((opts or {}).get("version")) if isinstance(opts, dict) else None
                if ver in ("latest", "lts", "os-provided", "none"):
                    ver = None
                ecos.append({"ecosystem": m.group(1), "version": ver, "package_manager": None, "source": source})
    return dc, ecos


def python_pm(paths: set[str], pyproject: dict[str, Any] | None) -> str:
    if "uv.lock" in paths or (pyproject or {}).get("tool", {}).get("uv") is not None:
        return "uv"
    if "poetry.lock" in paths or (pyproject or {}).get("tool", {}).get("poetry") is not None:
        return "poetry"
    if "Pipfile.lock" in paths:
        return "pipenv"
    return "pip"


def node_pm(paths: set[str], package_json: dict[str, Any] | None) -> str:
    pm = str((package_json or {}).get("packageManager") or "").split("@", 1)[0]
    if pm in ("npm", "pnpm", "yarn"):
        return pm
    if "pnpm-lock.yaml" in paths:
        return "pnpm"
    if "yarn.lock" in paths:
        return "yarn"
    return "npm"


# ------------------------------------------------------------------------------------------------ Claude config
def parse_claude_settings(text_: str) -> tuple[list[dict[str, Any]], int]:
    """(hooks, number of permission rules) of .claude/settings.json."""
    doc = load_jsonc(text_)
    if not isinstance(doc, dict):
        raise ValueError("not an object")
    hooks = []
    for event, groups in (doc.get("hooks") or {}).items():
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and h.get("type", "command") == "command" and h.get("command"):
                    cmd = str(h["command"])
                    hooks.append({"event": str(event), "matcher": g.get("matcher"), "command": cmd[:1000],
                                  "command_hash": _sha256(cmd)})
    perms = doc.get("permissions") or {}
    n = sum(len(perms.get(k) or []) for k in ("allow", "deny", "ask")) if isinstance(perms, dict) else 0
    return hooks, n


def claude_names(paths: list[str]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {k: set() for k in CLAUDE_DIRS}
    for p in paths:
        for k in CLAUDE_DIRS:
            pre = f".claude/{k}/"
            if not p.startswith(pre):
                continue
            rest = p[len(pre):]
            if k == "skills":
                out[k].add(rest.split("/", 1)[0])
            elif k == "rules":
                out[k].add(rest)
            elif rest.endswith(".md"):
                out[k].add(rest[:-3])
    return {k: sorted(v) for k, v in out.items()}


PKG_RUNNERS = {"npx", "uvx", "pipx", "bunx", "pnpm", "yarn", "bun", "deno"}
SUBCOMMANDS = {"run", "dlx", "exec", "x"}
_OPT_WITH_VALUE = {"--from", "-p", "--package", "--with"}


def mcp_package(command: str, args: list[str]) -> str:
    """The package (or script/module/image) a stdio server runs: `npx -y @acme/x@1.2.0` -> `@acme/x@1.2.0`,
    `uvx --from pkg==1.0 tool` -> `pkg==1.0`, `python -m mod` -> `mod`, `docker run -i --rm img:1` -> `img:1`."""
    base = posixpath.basename(command)
    a = [str(x) for x in args]
    if base in ("docker", "podman"):
        imgs = [x for x in a[1:] if not x.startswith("-") and ("/" in x or ":" in x) and "=" not in x]
        return imgs[-1] if imgs else ""
    i = 0
    while i < len(a):
        x = a[i]
        if x in _OPT_WITH_VALUE and i + 1 < len(a):
            return a[i + 1]
        if x == "-m" and i + 1 < len(a):
            return a[i + 1]
        if x.startswith("-") or (base in PKG_RUNNERS and x in SUBCOMMANDS):
            i += 1
            continue
        return x
    return ""


def origin(url: str) -> str:
    u = urlsplit(url)
    if not u.scheme or not u.hostname:
        return url[:200]
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://{u.hostname}{port}"


def parse_mcp(text_: str) -> list[dict[str, Any]]:
    doc = load_jsonc(text_)
    servers = (doc or {}).get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("no mcpServers object")
    out = []
    for name, sv in servers.items():
        if not isinstance(sv, dict):
            continue
        url = sv.get("url") if isinstance(sv.get("url"), str) else None
        command = str(sv.get("command") or "") or None
        args = [str(x) for x in sv.get("args") or []] if isinstance(sv.get("args"), list) else []
        transport = str(sv.get("type") or ("stdio" if command else "http" if url else "unknown"))
        if url and transport == "stdio":
            transport = "http"
        if transport == "stdio":
            identity = f"stdio:{posixpath.basename(command or '')}:{mcp_package(command or '', args)}"
        else:
            identity = f"url:{origin(url or '')}"
        out.append({"name": str(name), "transport": transport, "command": command, "args": args[:30],
                    "url": origin(url) if url else None,
                    "env_keys": sorted(str(k) for k in (sv.get("env") or {})) if isinstance(sv.get("env"), dict)
                    else [],
                    "header_keys": sorted(str(k) for k in (sv.get("headers") or {}))
                    if isinstance(sv.get("headers"), dict) else [],
                    "identity": identity[:300]})
    return out


def parse_pre_commit(text_: str) -> list[str]:
    doc = yaml.safe_load(text_) or {}
    ids = []
    for r in doc.get("repos") or []:
        for h in (r or {}).get("hooks") or []:
            if isinstance(h, dict) and h.get("id"):
                ids.append(str(h["id"]))
    return ids


# ------------------------------------------------------------------------------------------------ profile
def build_profile(meta: dict[str, Any], tree: list[dict[str, Any]], contents: dict[str, str | None]) -> dict[str, Any]:
    """The contract §1 profile from the tree at `meta.sha` and the contents of `to_read(...)` (None: unreadable)."""
    paths = [e["path"] for e in tree]
    pset = set(paths)
    size = {e["path"]: int(e.get("size") or 0) for e in tree}
    errors: list[dict[str, str]] = []
    files: list[dict[str, Any]] = []
    for p in sorted(p for p in paths if watched(p)):
        if p.startswith(".claude/") and p.split("/")[1] in CLAUDE_DIRS:
            continue                                         # names only (claude.rules/skills/...)
        txt = contents.get(p)
        files.append({"path": p, "sha256": _sha256(txt) if txt is not None else None, "bytes": size.get(p, 0)})

    def parse(path: str, fn: Callable[[str], Any], default: Any) -> Any:
        txt = contents.get(path)
        if txt is None:
            return default
        try:
            return fn(txt)
        except Exception as e:  # noqa: BLE001 - a broken file is recorded, never fatal
            msg = "invalid JSON" if isinstance(e, ValueError) and path.endswith(".json") else \
                f"{type(e).__name__}: {str(e).splitlines()[0][:150] if str(e) else ''}"
            errors.append({"path": path, "error": msg.strip()})
            return default

    ecos: list[dict[str, Any]] = []
    cmds: list[dict[str, Any]] = []
    for p in sorted(p for p in contents if WORKFLOW_RE.match(p)):
        c, e = parse(p, lambda t, p=p: parse_github_workflow(p, t), ([], []))
        cmds += c
        ecos += e
    c, e = parse(".gitlab-ci.yml", parse_gitlab_ci, ([], []))
    cmds += c
    ecos += e
    devcontainer = None
    for dcp in (".devcontainer/devcontainer.json", ".devcontainer.json"):
        if contents.get(dcp) is not None:
            dc, e = parse(dcp, lambda t, dcp=dcp: parse_devcontainer(t, dcp), (None, []))
            devcontainer = devcontainer or dc
            ecos += e
    ecos += parse(".tool-versions", parse_tool_versions, [])
    pyproject = parse("pyproject.toml", tomllib.loads, None)
    package_json = parse("package.json", json.loads, None)
    if not isinstance(package_json, dict):
        package_json = None
    if "go.mod" in pset:
        ecos.append(parse("go.mod", parse_go_mod, {"ecosystem": "go", "version": None, "package_manager": "go",
                                                   "source": "go.mod"}))
    py_pm = python_pm(pset, pyproject)
    if contents.get(".python-version"):
        ecos.append({"ecosystem": "python", "version": _version_str(contents[".python-version"].splitlines()[0]
                                                                    if contents[".python-version"].strip() else None),
                     "package_manager": py_pm, "source": ".python-version"})
    if "pyproject.toml" in pset:
        req = ((pyproject or {}).get("project") or {}).get("requires-python")
        ecos.append({"ecosystem": "python", "version": str(req)[:40] if req else None, "package_manager": py_pm,
                     "source": "pyproject.toml"})
    reqs = sorted(p for p in paths if REQUIREMENTS_RE.match(p))
    if reqs and "pyproject.toml" not in pset:
        ecos.append({"ecosystem": "python", "version": None, "package_manager": py_pm, "source": reqs[0]})
    n_pm = node_pm(pset, package_json)
    if contents.get(".nvmrc"):
        v = _version_str(contents[".nvmrc"].strip().splitlines()[0]) if contents[".nvmrc"].strip() else None
        ecos.append({"ecosystem": "node", "version": None if v and v.startswith("lts") else v, "package_manager": n_pm,
                     "source": ".nvmrc"})
    if "package.json" in pset:
        eng = (package_json or {}).get("engines") or {}
        scripts = (package_json or {}).get("scripts") or {}
        ecos.append({"ecosystem": "node", "version": _version_str(eng.get("node")) if isinstance(eng, dict) else None,
                     "package_manager": n_pm, "source": "package.json",
                     "scripts": sorted(str(k) for k in scripts)[:100] if isinstance(scripts, dict) else []})
    for e in ecos:                                   # fill the package manager from the repo's lockfiles
        if e.get("package_manager") is None:
            e["package_manager"] = {"python": py_pm, "node": n_pm, "go": "go"}.get(e["ecosystem"])

    hooks, n_perm = parse(".claude/settings.json", parse_claude_settings, ([], 0))
    names = claude_names(paths)
    claude_md = sorted(p for p in paths if posixpath.basename(p) == "CLAUDE.md")
    mcp = parse(".mcp.json", parse_mcp, [])
    pre_commit_hooks = parse(".pre-commit-config.yaml", parse_pre_commit, [])
    seen: set[tuple[str, str]] = set()
    uniq_cmds = []
    for c in cmds:
        if (c["stage"], c["command"]) not in seen:
            seen.add((c["stage"], c["command"]))
            uniq_cmds.append(c)
    return {
        "repo_id": str(meta["repo_id"]), "full_name": meta["full_name"], "default_branch": meta["default_branch"],
        "sha": meta["sha"], "scanned_at": meta.get("scanned_at") or dt.datetime.now(dt.UTC).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "scanner_version": SCANNER_VERSION, "files": files, "ecosystems": ecos, "ci_commands": uniq_cmds[:100],
        "devcontainer": devcontainer,
        "claude": {"settings_present": ".claude/settings.json" in pset, "hooks": hooks, **names,
                   "claude_md": claude_md, "permission_rules": n_perm},
        "agents_md": "AGENTS.md" in pset, "copilot_instructions": ".github/copilot-instructions.md" in pset,
        "mcp_servers": mcp,
        "pre_commit": {"present": ".pre-commit-config.yaml" in pset, "hooks": pre_commit_hooks},
        "errors": errors,
    }


async def scan(g: Any, owner: str, name: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Read the repo at `meta.sha` through `g` (a GiteaClient) and build its profile."""
    tree = await g.tree_entries(owner, name, meta["sha"])
    contents: dict[str, str | None] = {}
    for p in to_read([e["path"] for e in tree]):
        contents[p] = await g.file_at(owner, name, meta["sha"], p, max_bytes=MAX_READ_BYTES)
    return build_profile(meta, tree, contents)


# ------------------------------------------------------------------------------------------------ scheduling
class Scanner:
    """Coalesced scan requests per repo, processed by `loop()` (one per process). `gitea(repo)` returns a GiteaClient
    with a read token; `put(repo_id, profile)` sends the profile to control-plane-api."""

    def __init__(self, gitea: Callable[[dict[str, Any]], Awaitable[Any]],
                 put: Callable[[str, dict[str, Any]], Awaitable[int]]) -> None:
        self._gitea = gitea
        self._put = put
        self.pending: dict[str, dict[str, Any]] = {}
        self.profiled: dict[str, str] = {}          # repo_id -> last sha sent from this process
        self._wake = asyncio.Event()

    def request(self, repo: dict[str, Any], reason: str) -> None:
        if not repo.get("repo_id") or not repo.get("full_name") or not repo.get("installation_id"):
            return
        self.pending[str(repo["repo_id"])] = {**repo, "reason": reason}
        self._wake.set()

    def known(self, repo_id: str) -> bool:
        return str(repo_id) in self.profiled

    async def scan_repo(self, repo: dict[str, Any]) -> dict[str, Any] | None:
        owner, _, name = str(repo["full_name"]).partition("/")
        g = await self._gitea(repo)
        async with g:
            branch_name = repo.get("default_branch") or "main"
            br = await g.get_branch(owner, name, branch_name)
            if br is None:
                info = await g.get_repo(owner, name)
                branch_name = info.get("default_branch") or branch_name
                br = await g.get_branch(owner, name, branch_name)
            sha = ((br or {}).get("commit") or {}).get("id")
            if not sha:
                log.info(json.dumps({"event": "inventory.scan_skipped", "repo_id": repo["repo_id"],
                                     "reason": "empty_repository"}))
                return None
            meta = {"repo_id": str(repo["repo_id"]), "full_name": repo["full_name"], "default_branch": branch_name,
                    "sha": sha}
            profile = await scan(g, owner, name, meta)
        status = await self._put(str(repo["repo_id"]), profile)
        if status >= 300:
            raise RuntimeError(f"profile PUT answered {status}")
        self.profiled[str(repo["repo_id"])] = sha
        log.info(json.dumps({"event": "inventory.scanned", "repo_id": repo["repo_id"], "sha": sha,
                             "reason": repo.get("reason"), "files": len(profile["files"]),
                             "errors": len(profile["errors"])}))
        return profile

    async def run_pending(self) -> int:
        n = 0
        while self.pending:
            repo_id = next(iter(self.pending))
            repo = self.pending.pop(repo_id)
            try:
                if await self.scan_repo(repo) is not None:
                    n += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - best effort; the next trigger or a rescan retries
                log.warning("inventory scan %s failed: %s: %s", repo_id, type(e).__name__, str(e)[:200])
        return n

    async def loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self.run_pending()
