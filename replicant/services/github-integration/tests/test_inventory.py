"""Inventory scanner (ONBOARD-01 §1): pure parsers and the Scanner against a fake GiteaClient (no DB, no network)."""
from __future__ import annotations

import json

import pytest

from github_integration import inventory as I

CI = """
name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: uv sync --frozen
      - run: |
          uv run pytest -q \\
            --maxfail=1
          echo done
      - run: uv run ruff check .
      - run: pytest ${{ matrix.args }}
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: uv run pytest -q
  docs:
    runs-on: ubuntu-latest
    defaults: {run: {working-directory: web}}
    steps:
      - uses: actions/setup-node@v4
        with: {node-version: 22, cache: pnpm}
      - run: pnpm install --frozen-lockfile
      - run: pnpm run build
      - run: docker build -t x .
      - run: git fetch origin
"""


def test_classify_ci_line():
    c = I.classify_ci_line
    assert c("uv run pytest -q") == "test" and c("go test -count=1 ./...") == "test" and c("pnpm test") == "test"
    assert c("npm run test:unit") == "test"
    assert c("uv run ruff check .") == "lint" and c("golangci-lint run") == "lint" and c("npm run lint") == "lint"
    assert c("go build ./...") == "build" and c("yarn build") == "build"
    assert c("npm ci") == "install" and c("pip install -r requirements.txt") == "install"
    assert c("go mod download") == "install" and c("uv sync --frozen") == "install"
    for dropped in ("docker build .", "./deploy.sh", "gh release create", "echo pytest", "pytest ${{ matrix.x }}",
                    "aws s3 cp x y", "make release", "# pytest", "", "codecov -t x", "npm publish"):
        assert c(dropped) is None, dropped
    assert c("CI=1 pnpm test") == "test"


def test_parse_github_workflow():
    cmds, ecos = I.parse_github_workflow(".github/workflows/ci.yml", CI)
    assert {"stage": "install", "command": "uv sync --frozen", "source": ".github/workflows/ci.yml#jobs.test"} in cmds
    assert {"stage": "test", "command": "uv run pytest -q --maxfail=1",
            "source": ".github/workflows/ci.yml#jobs.test"} in cmds
    assert {"stage": "lint", "command": "uv run ruff check .", "source": ".github/workflows/ci.yml#jobs.test"} in cmds
    assert not any("deploy" in c["source"] for c in cmds)              # deploy job dropped
    assert not any("matrix" in c["command"] or "docker" in c["command"] or "git " in c["command"] for c in cmds)
    assert {"stage": "build", "command": "cd web && pnpm run build",
            "source": ".github/workflows/ci.yml#jobs.docs"} in cmds
    assert {"ecosystem": "python", "version": "3.12", "package_manager": "uv",
            "source": ".github/workflows/ci.yml#jobs.test"} in ecos
    assert {"ecosystem": "node", "version": "22", "package_manager": "pnpm",
            "source": ".github/workflows/ci.yml#jobs.docs"} in ecos


def test_parse_gitlab_ci():
    cmds, ecos = I.parse_gitlab_ci("""
image: python:3.13-slim
stages: [test, deploy]
.template: {script: [pytest]}
unit:
  stage: test
  before_script: ["pip install -r requirements.txt"]
  script: ["python -m pytest -q"]
lint:
  image: {name: "node:22"}
  script: npm run lint
deploy_prod:
  stage: deploy
  script: ["pytest"]
""")
    assert [(c["stage"], c["command"], c["source"]) for c in cmds] == [
        ("install", "pip install -r requirements.txt", ".gitlab-ci.yml#unit"),
        ("test", "python -m pytest -q", ".gitlab-ci.yml#unit"),
        ("lint", "npm run lint", ".gitlab-ci.yml#lint")]
    assert {(e["ecosystem"], e["version"], e["source"]) for e in ecos} == {
        ("python", "3.13", ".gitlab-ci.yml#unit"), ("node", "22", ".gitlab-ci.yml#lint")}


def test_version_files_and_images():
    assert I.parse_tool_versions("python 3.12.4\nnodejs 22.1.0 # x\nruby 3\ngolang 1.26") == [
        {"ecosystem": "python", "version": "3.12.4", "package_manager": None, "source": ".tool-versions"},
        {"ecosystem": "node", "version": "22.1.0", "package_manager": None, "source": ".tool-versions"},
        {"ecosystem": "go", "version": "1.26", "package_manager": None, "source": ".tool-versions"}]
    assert I.parse_go_mod("module x\n\ngo 1.22.3\n\nrequire y v1\n")["version"] == "1.22.3"
    src = ".devcontainer/devcontainer.json"
    assert I.ecosystem_of_image("mcr.microsoft.com/devcontainers/python:1-3.12-bookworm", src)["version"] == "3.12"
    assert I.ecosystem_of_image("mcr.microsoft.com/devcontainers/typescript-node:1-22", src)["ecosystem"] == "node"
    assert I.ecosystem_of_image("golang:1.26", src) == {"ecosystem": "go", "version": "1.26",
                                                       "package_manager": None, "source": src}
    assert I.ecosystem_of_image("ubuntu:24.04", src) is None


def test_devcontainer_jsonc():
    dc, ecos = I.parse_devcontainer("""{
      // the dev image
      "image": "mcr.microsoft.com/devcontainers/python:1-3.13",
      "features": {"ghcr.io/devcontainers/features/node:1": {"version": "22"},
                   "ghcr.io/devcontainers/features/github-cli:1": {},},
      /* trailing commas */
      "postCreateCommand": "echo 'http://not-a-comment'",
    }""", ".devcontainer/devcontainer.json")
    assert dc == {"image": "mcr.microsoft.com/devcontainers/python:1-3.13",
                  "features": ["ghcr.io/devcontainers/features/github-cli:1", "ghcr.io/devcontainers/features/node:1"]}
    assert [(e["ecosystem"], e["version"]) for e in ecos] == [("python", "3.13"), ("node", "22")]


def test_claude_settings_and_names():
    hooks, n = I.parse_claude_settings(json.dumps({
        "hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command",
                                                                      "command": "uv run ruff format $FILE"}]}],
                  "Stop": [{"hooks": [{"type": "command", "command": "make check"}, {"type": "prompt", "prompt": "x"}]}]},
        "permissions": {"allow": ["Bash(ls)"], "deny": ["Read(.env)", "Bash(curl:*)"]}}))
    assert n == 3 and [h["event"] for h in hooks] == ["PostToolUse", "Stop"]
    assert hooks[0]["matcher"] == "Edit|Write" and hooks[0]["command_hash"].startswith("sha256:")
    import hashlib  # contract §6: sha256 of the exact command string, no normalisation (matches the entrypoint)
    assert hooks[0]["command_hash"] == "sha256:" + hashlib.sha256(b"uv run ruff format $FILE").hexdigest()
    names = I.claude_names([".claude/rules/python-style.md", ".claude/skills/migrations/SKILL.md",
                            ".claude/skills/migrations/ref.md", ".claude/commands/fix.md", ".claude/agents/rev.md",
                            ".claude/settings.json"])
    assert names == {"rules": ["python-style.md"], "skills": ["migrations"], "commands": ["fix"], "agents": ["rev"]}


def test_mcp_parsing_sends_keys_not_values():
    servers = I.parse_mcp(json.dumps({"mcpServers": {
        "schema": {"command": "npx", "args": ["-y", "@acme/schema-mcp@1.2.0"]},
        "jira": {"type": "http", "url": "https://jira.acme.internal/mcp?x=1", "headers": {"Authorization": "Bearer s3"}},
        "db": {"command": "uvx", "args": ["--from", "pg-mcp==0.4.1", "pg-mcp"], "env": {"PG_PASSWORD": "hunter2"}},
        "local": {"command": "python", "args": ["-m", "tools.mcp"]},
        "img": {"command": "docker", "args": ["run", "-i", "--rm", "-e", "X=1", "ghcr.io/acme/mcp:2"]}}}))
    by = {s["name"]: s for s in servers}
    assert by["schema"]["identity"] == "stdio:npx:@acme/schema-mcp@1.2.0" and by["schema"]["transport"] == "stdio"
    assert by["jira"]["identity"] == "url:https://jira.acme.internal" and by["jira"]["header_keys"] == ["Authorization"]
    assert by["db"]["identity"] == "stdio:uvx:pg-mcp==0.4.1" and by["db"]["env_keys"] == ["PG_PASSWORD"]
    assert by["local"]["identity"] == "stdio:python:tools.mcp"
    assert by["img"]["identity"] == "stdio:docker:ghcr.io/acme/mcp:2"
    dumped = json.dumps(servers)
    assert "hunter2" not in dumped and "Bearer s3" not in dumped and "x=1" not in dumped


def test_push_watch():
    assert I.push_touches_watched({"commits": [{"modified": ["src/billing/CLAUDE.md"]}], "total_commits": 1})
    assert I.push_touches_watched({"commits": [{"added": [".claude/rules/x.md"]}]})
    assert not I.push_touches_watched({"commits": [{"modified": ["src/app.py"], "added": ["docs/x.md"]}],
                                       "total_commits": 1})
    assert I.push_touches_watched({"commits": [{"modified": ["src/app.py"]}], "total_commits": 40})  # truncated
    assert I.push_touches_watched({"commits": []})
    for p in ("uv.lock", "requirements-dev.txt", ".github/workflows/ci.yaml", ".mcp.json", "go.mod"):
        assert I.watched(p), p
    assert not I.watched("src/go.mod.bak") and not I.watched("pkg/package.json")


REPO_FILES = {
    "CLAUDE.md": "# Billing\n", "src/billing/CLAUDE.md": "nested", "AGENTS.md": "agents",
    ".python-version": "3.12\n", "pyproject.toml": '[project]\nname="b"\nrequires-python=">=3.12"\n',
    "uv.lock": None, ".github/workflows/ci.yml": CI,
    ".claude/settings.json": json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [
        {"type": "command", "command": "uv run ruff format"}]}]}}),
    ".claude/rules/python-style.md": None, ".mcp.json": "{not json", "src/app.py": "print(1)",
    ".pre-commit-config.yaml": "repos:\n- repo: x\n  hooks: [{id: ruff}, {id: end-of-file-fixer}]\n",
    "logo.png": None,
}


class FakeGitea:
    def __init__(self, files: dict[str, str | None], sha: str = "a" * 40) -> None:
        self.files, self.sha, self.reads = files, sha, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get_branch(self, owner, name, branch):
        return {"name": branch, "commit": {"id": self.sha}} if branch == "main" else None

    async def get_repo(self, owner, name):
        return {"default_branch": "main"}

    async def tree_entries(self, owner, name, sha):
        assert sha == self.sha
        return [{"path": p, "size": len(c or "x" * 10), "sha": "g"} for p, c in self.files.items()]

    async def file_at(self, owner, name, sha, path, *, max_bytes=65536):
        self.reads.append(path)
        return self.files.get(path)


def test_build_profile_from_fake_repo():
    g = FakeGitea(REPO_FILES)
    import asyncio
    prof = asyncio.run(I.scan(g, "acme-payments", "billing-py", {"repo_id": "9", "full_name": "acme-payments/billing-py",
                                                                  "default_branch": "main", "sha": g.sha}))
    assert "src/app.py" not in g.reads and "uv.lock" not in g.reads and "logo.png" not in g.reads
    assert prof["sha"] == g.sha and prof["scanner_version"] == 1 and prof["repo_id"] == "9"
    files = {f["path"]: f for f in prof["files"]}
    assert files["CLAUDE.md"]["sha256"].startswith("sha256:") and files["uv.lock"]["sha256"] is None
    assert "src/app.py" not in files and ".claude/rules/python-style.md" not in files
    assert {"ecosystem": "python", "version": "3.12", "package_manager": "uv", "source": ".python-version"} \
        in prof["ecosystems"]
    assert {"ecosystem": "python", "version": ">=3.12", "package_manager": "uv", "source": "pyproject.toml"} \
        in prof["ecosystems"]
    assert any(c["command"] == "uv run ruff check ." for c in prof["ci_commands"])
    cl = prof["claude"]
    assert cl["settings_present"] and cl["rules"] == ["python-style.md"] and cl["claude_md"] == [
        "CLAUDE.md", "src/billing/CLAUDE.md"] and cl["hooks"][0]["command"] == "uv run ruff format"
    assert prof["agents_md"] is True and prof["copilot_instructions"] is False
    assert prof["mcp_servers"] == [] and prof["errors"] == [{"path": ".mcp.json", "error": "invalid JSON"}]
    assert prof["pre_commit"] == {"present": True, "hooks": ["ruff", "end-of-file-fixer"]}


def test_node_profile_package_manager_and_scripts():
    files = {"package.json": json.dumps({"packageManager": "pnpm@9.1.0", "engines": {"node": ">=22"},
                                         "scripts": {"test": "vitest", "build": "vite build"}}),
             "pnpm-lock.yaml": None, ".nvmrc": "v22.11.0\n"}
    prof = I.build_profile({"repo_id": "1", "full_name": "a/b", "default_branch": "main", "sha": "b" * 40},
                           [{"path": p, "size": 1} for p in files], files)
    node = [e for e in prof["ecosystems"] if e["ecosystem"] == "node"]
    assert {"ecosystem": "node", "version": "22.11.0", "package_manager": "pnpm", "source": ".nvmrc"} in node
    assert {"ecosystem": "node", "version": ">=22", "package_manager": "pnpm", "source": "package.json",
            "scripts": ["build", "test"]} in node


async def test_scanner_coalesces_and_puts():
    g = FakeGitea(REPO_FILES)
    puts = []

    async def gitea(repo):
        return g

    async def put(repo_id, profile):
        puts.append((repo_id, profile["sha"]))
        return 200 if repo_id != "bad" else 500
    sc = I.Scanner(gitea, put)
    repo = {"repo_id": "9", "installation_id": 1, "full_name": "acme-payments/billing-py", "default_branch": "main"}
    sc.request(repo, "registered")
    sc.request(repo, "push")                                   # coalesced
    sc.request({**repo, "repo_id": "bad"}, "push")             # PUT fails: logged, the next repo still runs
    sc.request({"repo_id": "x"}, "push")                       # incomplete: ignored
    assert await sc.run_pending() == 1
    assert puts == [("9", g.sha), ("bad", g.sha)] and sc.known("9") and not sc.known("bad")
    sc.request({**repo, "default_branch": "trunk"}, "default_branch_changed")   # stale branch: resolved via the repo
    assert await sc.run_pending() == 1


@pytest.mark.parametrize("payload,expected", [
    ({"commits": [{"modified": ["README.md"]}], "total_commits": 1}, False),
    ({"commits": [{"modified": ["package.json"]}], "total_commits": 1}, True),
])
def test_push_payloads(payload, expected):
    assert I.push_touches_watched(payload) is expected
