"""`replicant dev seed` for v0 (stack.md §5, reduced to M1). Every write goes through control-plane-api's admin/seed
API with the dev_seed_token (principal svc_dev-seeder, permission dev:seed); no SQL. Steps are idempotent and
recorded with PUT /v1/admin/dev/seed-status/{step} so a partial seed resumes. Gitea is only *read* here (repo id,
webhook check); gitea-bootstrap created it.

Operations used (spec/07-control-plane-and-ux/api/control-plane-api.openapi.yaml and
spec/06-releases-and-evals/api/releases.openapi.yaml):
  GET  /v1/admin/dev/seed-status                         getSeedStatus
  PUT  /v1/admin/dev/seed-status/{step}                   putSeedStatus
  PUT  /v1/admin/platform-caps                            putPlatformCaps
  POST /v1/admin/tenants                                  createTenant
  PUT  /v1/tenants/{tenant_id}/github/repos/{repo_id}/settings   putRepoSettings
  POST /v1/registry/publish                               registryPublish
  POST /v1/registry/bundles:build                         buildBundle
  POST /v1/releases/{env}/assignment:bootstrap            bootstrapAssignment
  POST /v1/admin/dev/release-bundles                      local-only M1 shortcut for seed steps 8-12 (CONTROL-02)
  github-integration POST /v1/admin/scm-installations     (stack.md §5 step 6; no host port, so it is called from
                                                           inside the github-integration container)
  identity links (stack.md §5 step 5)                     identity-service is not in v0, so `07-identity-links` writes
                                                           identity.users + identity.links as the identity-service DB
                                                           role inside postgres-replicant (DEVIATIONS NOTIFY-D5)
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from replicant_cli.paths import Paths

PLATFORM_CAPS = {  # stack.md §5 step 2 (local values)
    "platform_daily_usd_max": 50, "platform_monthly_usd_max": 500,
    "tenant_daily_usd_default": 10, "tenant_monthly_usd_default": 100,
}
TENANTS = [  # stack.md §5 step 2
    {"tenant_id": "payments", "display_name": "Payments"},
    {"tenant_id": "local-dev", "display_name": "Local development"},
    {"tenant_id": "replicant-eval", "display_name": "Replicant evaluation"},
]
GITEA_HOST = "git.replicant.localhost:3300"
ORG, REPO, TENANT = "acme-payments", "ledger-api", "payments"
WEBHOOK_URL = "http://github-integration:8080/webhooks/gitea"
WEBHOOK_EVENTS = {"issues", "issue_comment", "pull_request"}


def idem(*parts: str) -> str:
    """Deterministic Idempotency-Key per step and payload, as an RFC 9651 sf-string (C-IDS §2)."""
    return '"seed-' + hashlib.sha256("\n".join(parts).encode()).hexdigest()[:40] + '"'


@dataclass
class Ctx:
    p: Paths
    api: httpx.Client
    gitea: httpx.Client
    log: Callable[[str], None]
    dry_run: bool = False
    state: dict | None = None


class SeedError(RuntimeError):
    pass


def _check(r: httpx.Response, what: str, ok: tuple[int, ...] = (200, 201, 204)) -> httpx.Response:
    if r.status_code not in ok:
        body = r.text[:500].replace("\n", " ")
        raise SeedError(f"{what}: HTTP {r.status_code} {body}")
    return r


def step_platform_caps(c: Ctx) -> str:
    _check(c.api.put("/v1/admin/platform-caps", json=PLATFORM_CAPS,
                     headers={"Idempotency-Key": idem("caps", json.dumps(PLATFORM_CAPS, sort_keys=True))}),
           "PUT /v1/admin/platform-caps")
    return "caps set"


def step_tenants(c: Ctx) -> str:
    for t in TENANTS:
        _check(c.api.post("/v1/admin/tenants", json=t, headers={"Idempotency-Key": idem("tenant", t["tenant_id"])}),
               f"POST /v1/admin/tenants {t['tenant_id']}")
    return ",".join(t["tenant_id"] for t in TENANTS)


def gitea_repo_id(c: Ctx) -> int:
    r = _check(c.gitea.get(f"/api/v1/repos/{ORG}/{REPO}"), f"Gitea GET repo {ORG}/{REPO}")
    return int(r.json()["id"])


def step_scm_installation(c: Ctx) -> str:
    """github-integration POST /v1/admin/scm-installations, run inside its container (it has no host port).
    The token travels on stdin, never in argv or the environment of the exec."""
    import subprocess

    from replicant_cli import compose
    cont = compose.containers().get("github-integration")
    if not cont or cont.status != "running":
        raise SeedError("github-integration is not running")
    body = json.dumps({"provider": "gitea", "host": GITEA_HOST, "org": ORG, "tenant_id": TENANT})
    script = (
        "import sys,json,urllib.request as u\n"
        "tok=sys.stdin.readline().strip()\n"
        f"r=u.Request('http://127.0.0.1:8080/v1/admin/scm-installations',data={body!r}.encode(),method='POST',"
        "headers={'Authorization':'Bearer '+tok,'Content-Type':'application/json','Idempotency-Key':'\"seed-scm-1\"'})\n"
        "try:\n r=u.urlopen(r,timeout=20); print(r.status, r.read()[:300].decode())\n"
        "except u.HTTPError as e: print(e.code, e.read()[:300].decode()); sys.exit(1)\n")
    r = subprocess.run(["podman", "exec", "-i", cont.name, "python", "-c", script],
                       input=c.p.secret("dev_seed_token") + "\n", capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SeedError(f"scm-installations: {(r.stdout + r.stderr).strip()[-400:]}")
    return f"gitea {GITEA_HOST} org {ORG} -> tenant {TENANT} ({r.stdout.strip()[:80]})"


def step_repo_binding(c: Ctx) -> str:
    rid = str(gitea_repo_id(c))
    body = {
        "repo_id": rid,
        "full_name": f"{ORG}/{REPO}",
        "enabled": True,
        "trigger_modes": ["label"],
        "trigger_label": "replicant",
        "allowed_workflow_types": ["bugfix"],
        "default_priority": "normal",
        "ack_comment": True,
    }
    _check(c.api.put(f"/v1/tenants/{TENANT}/github/repos/{rid}/settings", json=body,
                     headers={"Idempotency-Key": idem("repo", rid), "X-Replicant-Scm-Provider": "gitea",
                              "X-Replicant-Scm-Host": GITEA_HOST}),
           "PUT repo settings", ok=(200, 201, 204, 404))
    return f"{GITEA_HOST}/{ORG}/{REPO} (gitea repo_id {rid}) -> tenant {TENANT}"


def _definition_docs(p: Paths) -> list[dict]:
    root = p.root / "dev" / "config-local" / "definitions"
    docs = []
    for f in sorted(root.rglob("*.yaml")) if root.is_dir() else []:
        docs.append({"path": str(f.relative_to(root)), "content_yaml": f.read_text()})
    return docs


def step_registry(c: Ctx) -> str:
    docs = _definition_docs(c.p)
    if not docs:
        return "no documents under dev/config-local/definitions (skipped)"
    digest = hashlib.sha256(json.dumps(docs, sort_keys=True).encode()).hexdigest()
    from replicant_ids import derived  # publish_id is client-generated and must be stable per content (C-DEF §4.2)
    body = {"publish_id": "pub_" + derived("x", digest).split("_", 1)[1], "commit_sha": digest[:40],
            "source": "config_repo", "dry_run": False, "documents": docs}
    r = _check(c.api.post("/v1/registry/publish", json=body, headers={"Idempotency-Key": idem("publish", digest)}),
               "POST /v1/registry/publish")
    items = r.json().get("items", []) if r.content else []
    return f"{len(docs)} documents published ({sum(1 for i in items if i.get('created'))} new)"


def step_bundle(c: Ctx) -> str:
    req_file = c.p.root / "dev" / "config-local" / "bundles" / "requests" / "local.yaml"
    request = yaml.safe_load(req_file.read_text())
    r = _check(c.api.post("/v1/registry/bundles:build", json={"request": request},
                          headers={"Idempotency-Key": idem("bundle", req_file.read_text())}),
               "POST /v1/registry/bundles:build")
    res = r.json()
    rb = res.get("release_bundle_id") or (res.get("bundle") or {}).get("release_bundle_id")
    if not rb:
        raise SeedError(f"bundles:build returned no release_bundle_id: {json.dumps(res)[:300]}")
    (c.state or {})["release_bundle_id"] = rb
    return rb


def step_dev_bundle(c: Ctx) -> str:
    """CONTROL-02: local-only shortcut registering a bundle pinned to the worker build and bootstrapping `local`.
    Falls back to the spec path (publish, build, assignment:bootstrap) if the route is absent."""
    import subprocess

    from replicant_cli import compose
    build_id = compose.env_values(c.p).get("REPLICANT_WORKER_BUILD_ID", "b20261024-000000000000")
    # CONTROL-07 / WORKER-03: the bundle spec pins exactly the definition blobs replicant-worker published at startup.
    fw = compose.containers().get("replicant-worker")
    if not fw or fw.status != "running":
        raise SeedError("replicant-worker is not running (its `local-bundle spec` defines the v0 bundle)")
    out = subprocess.run(["podman", "exec", fw.name, "replicant-worker", "local-bundle", "spec"], capture_output=True,
                         text=True, timeout=60)
    if out.returncode != 0:
        raise SeedError(f"replicant-worker local-bundle spec: {out.stderr.strip()[-300:]}")
    spec = json.loads(out.stdout)
    # make_default: a re-run after the worker's local definitions changed (`seed --only 05-bundle-bootstrap --force`)
    # moves the local default to the new bundle (dev-only stand-in for promote + set_default, NOTIFY-D16)
    body = {"worker_build_id": build_id, "spec": spec, "bootstrap": True, "make_default": True}
    r = c.api.post("/v1/admin/dev/release-bundles", json=body,
                   headers={"Idempotency-Key": idem("dev-bundle", json.dumps(spec, sort_keys=True))})
    if r.status_code == 404:
        c.log("  /v1/admin/dev/release-bundles absent; using publish + bundles:build + assignment:bootstrap")
        step_registry(c)
        return step_bundle_bootstrap(c)
    res = _check(r, "POST /v1/admin/dev/release-bundles").json()
    return f"{res.get('release_bundle_id')} ({res.get('promotion_state')}), worker build {build_id}"


def step_bundle_bootstrap(c: Ctx) -> str:
    rb = (c.state or {}).get("release_bundle_id") or step_bundle(c)
    r = c.api.post("/v1/releases/local/assignment:bootstrap",
                   json={"release_bundle_id": rb, "reason": "replicant dev seed: v0 first local default"},
                   headers={"Idempotency-Key": idem("bootstrap", rb)})
    if r.status_code == 409:
        return f"assignment already exists (409); default unchanged ({rb} built)"
    _check(r, "POST /v1/releases/local/assignment:bootstrap")
    return f"local default = {rb}"


def step_gitea_webhook(c: Ctx) -> str:
    r = _check(c.gitea.get(f"/api/v1/orgs/{ORG}/hooks"), "Gitea GET org hooks")
    hooks = [h for h in r.json() if (h.get("config") or {}).get("url") == WEBHOOK_URL]
    if not hooks:
        raise SeedError(f"no org webhook to {WEBHOOK_URL} (gitea-bootstrap did not run?)")
    h = hooks[0]
    missing = WEBHOOK_EVENTS - set(h.get("events") or [])
    if not h.get("active") or missing or (h.get("config") or {}).get("content_type") != "json":
        raise SeedError(f"webhook {h.get('id')} misconfigured: active={h.get('active')} missing events={sorted(missing)}")
    labels = _check(c.gitea.get(f"/api/v1/repos/{ORG}/{REPO}/labels"), "Gitea GET labels").json()
    if not any(lbl.get("name") == "replicant" for lbl in labels):
        raise SeedError("label `replicant` missing on acme-payments/ledger-api")
    return f"org hook {h.get('id')} -> {WEBHOOK_URL} active, label replicant present"


DEV_LOGINS = ("grace", "carol", "alice", "bob", "dave", "erin")   # replicant_dev_auth static users
SLACK_TEAM = "T0LOCAL01"


def _sql_lit(v: object) -> str:
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def step_identity_links(c: Ctx) -> str:
    """stack.md §5 step 5: Gitea user ids and slack-mock user ids linked to each dev principal
    (derived("usr", "replicant-dev-user", login), the rule control-plane-api and github-integration use).
    identity-service (the writer of `identity`) is not in v0: the rows are written as its DB role
    `svc_identity_service` through psql in postgres-replicant (no secret leaves the container)."""
    import subprocess

    from replicant_ids import derived

    from replicant_cli import compose
    ws = yaml.safe_load((c.p.compose_dir / "config" / "slack-mock" / "workspace.yaml").read_text()) or {}
    team = (ws.get("team") or {}).get("id", SLACK_TEAM)
    slack = {u["name"]: u["id"] for u in ws.get("users") or [] if not u.get("is_bot")}
    stmts = ["BEGIN;", "SET LOCAL ROLE svc_identity_service;"]
    linked = []
    for login in DEV_LOGINS:
        pid = derived("usr", "replicant-dev-user", login)
        stmts.append(
            "INSERT INTO identity.users (principal_id, okta_sub, user_name, display_name, email, status) VALUES "
            f"({_sql_lit(pid)}, {_sql_lit('dev|' + login)}, {_sql_lit(login + '@acme.test')}, "
            f"{_sql_lit(login.capitalize())}, {_sql_lit(login + '@acme.test')}, 'active') ON CONFLICT (principal_id) DO NOTHING;")
        links = []
        r = c.gitea.get(f"/api/v1/users/{login}")
        if r.status_code == 200:
            links.append(("gitea", GITEA_HOST, str(r.json()["id"]), login))
        if login in slack:
            links.append(("slack", team, slack[login], login))
        for kind, ns, ext, lg in links:
            lid = derived("lnk", kind, ns, ext)
            stmts.append(
                "INSERT INTO identity.links (link_id, principal_id, kind, namespace, external_id, login, method, status, "
                f"verified_at) SELECT {_sql_lit(lid)}, {_sql_lit(pid)}, {_sql_lit(kind)}, {_sql_lit(ns)}, {_sql_lit(ext)}, "
                f"{_sql_lit(lg)}, 'dev_seed', 'active', now() WHERE NOT EXISTS (SELECT 1 FROM identity.links WHERE "
                f"kind = {_sql_lit(kind)} AND namespace = {_sql_lit(ns)} AND external_id = {_sql_lit(ext)} AND "
                "status IN ('pending', 'active'));")
            linked.append(f"{login}:{kind}")
    stmts.append("COMMIT;")
    cont = compose.containers().get("postgres-replicant")
    if not cont or cont.status != "running":
        raise SeedError("postgres-replicant is not running")
    r = subprocess.run(["podman", "exec", "-i", cont.name, "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", "postgres",
                        "-d", "replicant"], input="\n".join(stmts), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SeedError(f"identity links: {(r.stdout + r.stderr).strip()[-400:]}")
    return f"{len(linked)} links ({', '.join(linked)})"


SLACK_APPROVAL_CHANNEL = "C0PAYAPPR1"     # slack-mock #payments-approvals (stack.md §5 step 7)


def step_slack_channel(c: Ctx) -> str:
    """stack.md §5 step 7: the tenant's Slack approval channel (operational config, ADR-013). Jobs of the tenant get
    one thread there (NOTIFY-D13). Quotas are restated because the PUT replaces the settings document."""
    body = {"quotas": {"daily_budget_usd": PLATFORM_CAPS["tenant_daily_usd_default"],
                       "monthly_budget_usd": PLATFORM_CAPS["tenant_monthly_usd_default"]},
            "approval_targets": {"slack": {"approval_channel": SLACK_APPROVAL_CHANNEL}}}
    _check(c.api.put(f"/v1/admin/tenants/{TENANT}/settings", json=body,
                     headers={"Idempotency-Key": idem("slack-channel", json.dumps(body, sort_keys=True))}),
           f"PUT /v1/admin/tenants/{TENANT}/settings")
    return f"{TENANT}: approval_channel {SLACK_APPROVAL_CHANNEL}"


STEPS: list[tuple[str, Callable[[Ctx], str]]] = [
    ("01-platform-caps", step_platform_caps),
    ("02-tenants", step_tenants),
    ("03-scm-installation", step_scm_installation),
    ("04-repo-binding", step_repo_binding),
    ("05-bundle-bootstrap", step_dev_bundle),
    ("06-gitea-webhook", step_gitea_webhook),
    ("07-identity-links", step_identity_links),
    ("08-slack-approval-channel", lambda c: step_slack_channel(c)),
]
ALWAYS_RUN = {"06-gitea-webhook"}


def clients(p: Paths, api_url: str, gitea_url: str) -> tuple[httpx.Client, httpx.Client]:
    tok = p.secret("dev_seed_token")
    api = httpx.Client(base_url=api_url, timeout=30, headers={"Authorization": f"Bearer {tok}"})
    gitea = httpx.Client(base_url=gitea_url, timeout=30, auth=("gitea-admin", p.secret("gitea_admin_password")))
    return api, gitea


def run(p: Paths, api_url: str, gitea_url: str, only: str | None, force: bool, dry_run: bool,
        log: Callable[[str], None]) -> int:
    api, gitea = clients(p, api_url, gitea_url)
    c = Ctx(p, api, gitea, log, dry_run, {})
    done: set[str] = set()
    if not dry_run:
        r = api.get("/v1/admin/dev/seed-status")
        if r.status_code == 200:
            done = {s["step"] for s in r.json().get("steps", []) if s.get("state") == "done"}
        else:
            log(f"seed-status unavailable (HTTP {r.status_code}); running every step")
    failures = 0
    for name, fn in STEPS:
        if only and only not in (name, name.split("-", 1)[1]):
            continue
        if name in done and not force and name not in ALWAYS_RUN:
            log(f"{name}: done (skipped)")
            continue
        if dry_run:
            log(f"{name}: would run {fn.__name__}")
            continue
        try:
            detail = fn(c)
            state = "done"
            log(f"{name}: {detail}")
        except (SeedError, httpx.HTTPError, OSError, KeyError, ValueError) as e:
            detail, state = str(e)[:1900], "failed"
            failures += 1
            log(f"{name}: FAILED {detail}")
        try:
            api.put(f"/v1/admin/dev/seed-status/{name}", json={"state": state, "detail": detail[:2000]})
        except httpx.HTTPError:
            pass
        if state == "failed" and name in ("01-platform-caps", "02-tenants"):
            break
    return 1 if failures else 0


def default_urls() -> tuple[str, str]:
    return (os.environ.get("REPLICANT_API_URL", "http://127.0.0.1:8401"),
            os.environ.get("REPLICANT_GITEA_URL", "http://127.0.0.1:3300"))


__all__ = ["run", "STEPS", "default_urls", "Path"]
