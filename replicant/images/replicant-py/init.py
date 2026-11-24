"""One-shot `init` of the local stack (compose service `init`, SIMPLIFY-D3). It replaces the former one-shots
migrate, s3-init and gitea-bootstrap, in that order, and is safe to re-run. It never prints a secret.

  migrate  `replicant-db migrate --to head --advisory-lock --verify-drift --ensure-roles` (D-26): advisory lock,
           roles and grants (WS-10), alembic upgrade head, drift check, ops.migration_runs.
  s3       the seven C-ART bucket classes for org/env, the ops bucket and local-langfuse, with WS-10's local
           versioning, encryption, lifecycle and Object Lock configs (was config/s3/init.sh; parity.md P-05).
  gitea    the Gitea part of seed step 4 (was config/gitea/bootstrap-v0.sh, PLATFORM-D6): user alice, org, team
           payments-maintainers, the sample repos pushed as replicant-bot, label `replicant`, org webhook to
           github-integration. The site admin and replicant-bot need the Gitea CLI, so the gitea service creates
           them itself (config/gitea/entrypoint-v0.sh) and reports healthy only afterwards.

Usage: python /opt/replicant/init.py [migrate] [s3] [gitea]   (default: all three)
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STEPS = ("migrate", "s3", "gitea")


def log(msg: str) -> None:
    print(f"init: {msg}", flush=True)


def _secret(env_name: str) -> str:
    return Path(os.environ[env_name]).read_text().strip()


def _wait(what: str, probe, tries: int = 60, every: float = 2.0) -> None:  # noqa: ANN001
    for _ in range(tries):
        try:
            if probe():
                return
        except Exception:  # noqa: BLE001 - not up yet
            pass
        time.sleep(every)
    raise SystemExit(f"init: {what} not reachable")


# ------------------------------------------------------------------------------------------------------- migrate
def migrate() -> None:
    cmd = ["replicant-db", "migrate", "--to", "head", "--advisory-lock", "--verify-drift", "--ensure-roles"]
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        raise SystemExit(f"init: migrate failed (exit {rc})")
    log("migrations at head")


# ------------------------------------------------------------------------------------------------------------ s3
def s3() -> None:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    c = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name=os.environ.get("AWS_DEFAULT_REGION"),
                     aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                     aws_secret_access_key=_secret("AWS_SECRET_ACCESS_KEY_FILE"),
                     config=Config(s3={"addressing_style": "path"}))
    pfx = f"{os.environ['REPLICANT_ORG']}-replicant-{os.environ['REPLICANT_ENV']}"
    cfg = Path(os.environ.get("REPLICANT_S3_LOCAL_CONFIGS", "/init/s3-local"))  # <class>.{versioning,...}.json
    _wait(f"S3 at {os.environ['S3_ENDPOINT']}", lambda: c.list_buckets() is not None)

    def load(name: str) -> dict:
        return json.loads((cfg / name).read_text())

    def mk(bucket: str, lock: bool = False) -> None:
        try:
            c.head_bucket(Bucket=bucket)
            log(f"exists {bucket}")
        except ClientError:
            c.create_bucket(Bucket=bucket, **({"ObjectLockEnabledForBucket": True} if lock else {}))
            log(f"created {bucket}")

    def soft(what: str, fn, **kw) -> None:  # noqa: ANN001 - settings the local S3 server may not support
        try:
            fn(**kw)
        except ClientError:
            log(f"{what} not accepted for {kw['Bucket']} (parity.md P-05)")

    for cls in ("artifacts", "transcripts", "session", "audit", "memory", "eval", "registry"):
        b = f"{pfx}-{cls}"
        mk(b, lock=cls == "audit")
        c.put_bucket_versioning(Bucket=b, VersioningConfiguration=load(f"{cls}.versioning.json"))
        soft("encryption config", c.put_bucket_encryption, Bucket=b,
             ServerSideEncryptionConfiguration=load(f"{cls}.encryption.json"))
        soft("lifecycle (the retention sweeper covers it)", c.put_bucket_lifecycle_configuration, Bucket=b,
             LifecycleConfiguration=load(f"{cls}.lifecycle.json"))
    # audit: Object Lock defaults from WS-10's local file (GOVERNANCE, 1 day). Prod is COMPLIANCE, 730 days (C-ART
    # 2.0.0); locally compliance mode would make objects undeletable for 2 years (parity.md P-05, P-37). No identity
    # holds s3:BypassGovernanceRetention; `replicant dev reset` deletes the store's volume instead.
    c.put_object_lock_configuration(Bucket=f"{pfx}-audit", ObjectLockConfiguration=load("audit.object-lock.json"))
    # ops bucket (C-ART 2.0.0, CCR-09-10): kill-switch mirror (control-plane-api) and the exec revocation feed
    # (runner-local, prefix exec-revocations/local/, 90-day expiry). Versioned, no Object Lock.
    mk(f"{pfx}-ops")
    c.put_bucket_versioning(Bucket=f"{pfx}-ops", VersioningConfiguration={"Status": "Enabled"})
    soft("lifecycle", c.put_bucket_lifecycle_configuration, Bucket=f"{pfx}-ops", LifecycleConfiguration={"Rules": [
        {"ID": "exec-revocations-90d", "Status": "Enabled", "Filter": {"Prefix": "exec-revocations/"},
         "Expiration": {"Days": 90}}]})
    # The legal bucket (<org>-replicant-<env>-legal, CCR-10-10) is deliberately not created locally (parity.md P-42).
    mk("local-langfuse")
    log("buckets ready")


# --------------------------------------------------------------------------------------------------------- gitea
def gitea() -> None:
    import httpx

    g = os.environ["GITEA_URL"].rstrip("/")
    org = os.environ.get("GITEA_ORG", "acme-payments")
    hook_url = os.environ["GITEA_WEBHOOK_URL"]
    api = httpx.Client(base_url=f"{g}/api/v1", auth=("gitea-admin", _secret("GITEA_ADMIN_PASSWORD_FILE")), timeout=30)
    _wait("gitea", lambda: httpx.get(f"{g}/api/healthz", timeout=5).status_code == 200)

    def need(r: httpx.Response, what: str) -> httpx.Response:
        if not r.is_success:
            raise SystemExit(f"init: gitea API {what} failed: HTTP {r.status_code}: {r.text[:300]}")
        return r

    # The site admin is created by the gitea service before it reports healthy; retry briefly for auth to settle.
    _wait("gitea admin login", lambda: api.get("/user").status_code == 200, tries=30)

    if api.get("/users/alice").status_code == 404:  # the E2E driver acts as alice through the admin Sudo header
        need(api.post("/admin/users", json={"username": "alice", "email": "alice@acme.test",
                                            "password": secrets.token_urlsafe(24), "must_change_password": False}),
             "create user alice")
    log("user alice ready")

    if api.get(f"/orgs/{org}").status_code == 404:
        need(api.post("/orgs", json={"username": org, "full_name": "Acme Payments", "visibility": "private"}),
             "create org")
    log(f"org {org} ready")

    found = need(api.get(f"/orgs/{org}/teams/search", params={"q": "payments-maintainers"}), "search teams").json()
    team = next((t["id"] for t in found.get("data") or [] if t.get("name") == "payments-maintainers"), None)
    if team is None:
        team = need(api.post(f"/orgs/{org}/teams", json={
            "name": "payments-maintainers", "permission": "write", "includes_all_repositories": True,
            "units": ["repo.code", "repo.issues", "repo.pulls", "repo.releases", "repo.wiki"]}), "create team").json()["id"]
    for u in ("replicant-bot", "alice"):
        need(api.put(f"/teams/{team}/members/{u}"), f"add {u} to team")
    log(f"team payments-maintainers ({team}) ready")

    for name in os.environ.get("GITEA_SAMPLE_REPOS", "ledger-api billing-py web-console").split():
        if api.get(f"/repos/{org}/{name}").status_code == 404:
            need(api.post(f"/orgs/{org}/repos", json={"name": name, "private": True, "default_branch": "main",
                                                      "auto_init": False}), f"create repo {name}")
        if api.get(f"/repos/{org}/{name}/branches/main").status_code == 200:
            log(f"repo {org}/{name} already has main")
        else:
            _push_sample(g, org, name)
            log(f"repo {org}/{name} pushed")
        labels = need(api.get(f"/repos/{org}/{name}/labels", params={"limit": 50}), "list labels").json()
        if not any(lb.get("name") == "replicant" for lb in labels):
            need(api.post(f"/repos/{org}/{name}/labels", json={
                "name": "replicant", "color": "#5319e7", "description": "Hand this issue to the replicant"}),
                "create label")
        log(f"label replicant on {org}/{name} ready")

    hooks = need(api.get(f"/orgs/{org}/hooks"), "list org hooks").json()
    if any((h.get("config") or {}).get("url") == hook_url for h in hooks):
        log("org webhook already present")
    else:
        # Events: spec stack.md §5 step 4, plus issue_label (Gitea delivers label changes only on that event).
        need(api.post(f"/orgs/{org}/hooks", json={
            "type": "gitea", "active": True, "branch_filter": "*",
            "events": ["issues", "issue_label", "issue_comment", "pull_request", "pull_request_review_approved",
                       "pull_request_review_rejected", "pull_request_review_comment", "pull_request_comment",
                       "pull_request_sync"],
            "config": {"url": hook_url, "content_type": "json",
                       "secret": _secret("GITEA_WEBHOOK_SECRET_FILE")}}), "create org webhook")
        log(f"org webhook -> {hook_url} created")
    log("gitea bootstrap done")


def _push_sample(g: str, org: str, name: str) -> None:
    """Initial commit of dev/sample-repos/<name> (without ISSUES/), pushed to main as replicant-bot."""
    with tempfile.TemporaryDirectory(prefix="repo.") as w:
        shutil.copytree(Path("/sample-repos") / name, w, dirs_exist_ok=True)
        shutil.rmtree(Path(w) / "ISSUES", ignore_errors=True)
        env = {**os.environ, "HOME": w, "GIT_TERMINAL_PROMPT": "0"}
        who = ["-c", "user.name=replicant-bot", "-c", "user.email=replicant-bot@acme.test"]
        # Credentials come from a helper reading the secret file, so the password is never in a URL or argv.
        helper = ("!f() { echo username=replicant-bot; printf 'password=%s\\n' "
                  f"\"$(cat {os.environ['GITEA_BOT_PASSWORD_FILE']})\"; }}; f")
        for args in (["init", "-q", "-b", "main"], [*who, "add", "-A"],
                     [*who, "commit", "-q", "-m", f"Initial import of {name}"],
                     ["-c", "credential.helper=", "-c", f"credential.helper={helper}", "push", "-q",
                      f"{g}/{org}/{name}.git", "main"]):
            r = subprocess.run(["git", *args], cwd=w, env=env, capture_output=True, text=True)
            if r.returncode != 0:
                step = next(a for a in args if a in ("init", "add", "commit", "push"))
                raise SystemExit(f"init: git {step} for {name} failed: {r.stderr.strip()[:300]}")


def main(argv: list[str]) -> int:
    steps = argv or list(STEPS)
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        raise SystemExit(f"init: unknown step(s) {', '.join(unknown)}; choose from {', '.join(STEPS)}")
    for s in STEPS:
        if s in steps:
            log(f"step {s}")
            globals()[s]()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
