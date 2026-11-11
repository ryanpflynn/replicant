"""Driver for smoke.sh (stdlib only). Never prints secrets.

    smoke.py seed   platform caps, tenants, local bundle + bootstrap, SCM installation, seed-status
    smoke.py run    open a Gitea issue as alice with label `replicant`; assert intake row, pinned workflow, ack comment;
                    then send a signed Gitea `pull_request` closed/merged webhook for the job's branch and assert the
                    `pr.event` Signal reached the workflow; check run-grants JWKS and /grants/revoked
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

SEC = os.path.join(os.environ["CTL_HOME"], "secrets")
CPA = "http://127.0.0.1:58401"
GI = "http://127.0.0.1:58080"
GRANTS = "http://127.0.0.1:58403"
GITEA = "http://127.0.0.1:53300"
ENGINE = os.environ.get("REPLICANT_CONTAINER_CLI", "podman")
ORG, REPO = "acme-payments", "ledger-api"


def secret(name: str) -> str:
    with open(os.path.join(SEC, name), encoding="utf-8") as f:
        return f.read().strip()


def call(method: str, url: str, body: object | None = None, headers: dict | None = None,
         raw: bytes | None = None) -> tuple[int, object]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                         **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt.strip().startswith(("{", "[")) else txt)
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, txt


def ok(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def psql(sql: str) -> str:
    return subprocess.run([ENGINE, "exec", "ctl-pg", "psql", "-U", "postgres", "-d", "replicant", "-Atc", sql],
                          check=True, capture_output=True, text=True).stdout.strip()


def gitea_admin() -> dict:
    tok = base64.b64encode(f"gitea-admin:{secret('gitea_admin_password')}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


def seed() -> None:
    h = {"Authorization": f"Bearer {secret('dev_seed_token')}", "Idempotency-Key": uuid.uuid4().hex}
    s, b = call("PUT", f"{CPA}/v1/admin/platform-caps", {"platform_daily_usd_max": 50, "platform_monthly_usd_max": 500,
                                                         "tenant_daily_usd_default": 10, "tenant_monthly_usd_default": 100}, h)
    ok(s == 200, f"PUT /v1/admin/platform-caps -> {s}")
    for t in ("payments", "local-dev", "replicant-eval"):
        s, b = call("POST", f"{CPA}/v1/admin/tenants", {"tenant_id": t, "display_name": t}, h)
        ok(s in (200, 201), f"POST /v1/admin/tenants {t} -> {s} {b.get('status') if isinstance(b, dict) else b}")
    s, b = call("POST", f"{CPA}/v1/admin/dev/release-bundles", {"bootstrap": True}, h)
    ok(s == 200 and b.get("promotion_state") == "promoted", f"POST /v1/admin/dev/release-bundles -> {s} "
       f"{b.get('release_bundle_id')} {b.get('promotion_state')} default={b.get('assignment', {}).get('default')}")
    s, b = call("POST", f"{GI}/v1/admin/scm-installations", {"provider": "gitea", "host": "git.replicant.localhost:3300",
                                                             "org": ORG, "tenant_id": "payments"}, h)
    ok(s in (200, 201), f"POST github-integration /v1/admin/scm-installations -> {s} installation_id="
       f"{b.get('installation_id') if isinstance(b, dict) else b}")
    s, repo = call("GET", f"{GITEA}/api/v1/repos/{ORG}/{REPO}", headers=gitea_admin())
    s, b = call("PUT", f"{CPA}/v1/tenants/payments/github/repos/{repo['id']}/settings",
                {"repo_id": str(repo["id"]), "full_name": f"{ORG}/{REPO}", "enabled": True, "trigger_modes": ["label"],
                 "trigger_label": "replicant", "allowed_workflow_types": ["bugfix"], "default_priority": "normal",
                 "ack_comment": True}, {**h, "X-Replicant-Scm-Provider": "gitea", "X-Replicant-Scm-Host": "git.replicant.localhost:3300"})
    ok(s == 200, f"PUT /v1/tenants/payments/github/repos/{repo['id']}/settings (proxied to github-integration) -> {s} "
       f"installation_id={b.get('installation_id') if isinstance(b, dict) else b}")
    root = os.path.join(os.path.dirname(__file__), "../../../../dev/config-local")
    docs = []
    for dp, _, fs in sorted(os.walk(os.path.join(root, "definitions"))):
        for fn in sorted(fs):
            if fn.endswith(".yaml"):
                with open(os.path.join(dp, fn), encoding="utf-8") as f:
                    docs.append({"path": os.path.relpath(os.path.join(dp, fn), os.path.join(root, "definitions")),
                                 "content_yaml": f.read()})
    if docs:
        dg = hashlib.sha256(json.dumps(docs, sort_keys=True).encode()).hexdigest()
        pub = "pub_" + "".join("0123456789abcdefghjkmnpqrstvwxyz"[int(c, 16) % 32] for c in dg[:26])
        s, b = call("POST", f"{CPA}/v1/registry/publish", {"publish_id": pub, "commit_sha": dg[:40], "source": "config_repo",
                                                         "dry_run": False, "documents": docs}, h)
        ok(s == 200, f"POST /v1/registry/publish ({len(docs)} dev/config-local documents) -> {s}")
        import yaml  # noqa: PLC0415 (available in the venv that runs the smoke)
        with open(os.path.join(root, "bundles/requests/local.yaml"), encoding="utf-8") as f:
            req = yaml.safe_load(f)
        s, b = call("POST", f"{CPA}/v1/registry/bundles:build", {"request": req}, h)
        ok(s in (200, 201), f"POST /v1/registry/bundles:build (dev/config-local/bundles/requests/local.yaml) -> {s} "
           f"{b.get('release_bundle_id') if isinstance(b, dict) else b} {b.get('promotion_state') if isinstance(b, dict) else ''}")
    s, b = call("PUT", f"{CPA}/v1/admin/dev/seed-status/02-tenants", {"state": "done"}, h)
    ok(s == 200, f"PUT /v1/admin/dev/seed-status/02-tenants -> {s}")


def run() -> None:
    adm = gitea_admin()
    s, labels = call("GET", f"{GITEA}/api/v1/repos/{ORG}/{REPO}/labels", headers=adm)
    lid = next(lb["id"] for lb in labels if lb["name"] == "replicant")
    s, repo = call("GET", f"{GITEA}/api/v1/repos/{ORG}/{REPO}", headers=adm)
    title = f"Refund ledger entries double-count FX fees for partial refunds ({uuid.uuid4().hex[:6]})"
    s, issue = call("POST", f"{GITEA}/api/v1/repos/{ORG}/{REPO}/issues",
                    {"title": title, "body": "Partial EUR refund reverses the whole FX fee.", "labels": [lid]},
                    {**adm, "Sudo": "alice"})
    ok(s == 201, f"Gitea: alice opened issue #{issue.get('number')} with label replicant (repo id {repo['id']})")
    dedupe = f"gitea:issue-label:{repo['id']}:{issue['number']}:replicant"
    row = ""
    for _ in range(60):
        row = psql(f"SELECT job_id||'|'||start_status||'|'||release_bundle_id||'|'||requester||'|'||submitted_by "
                   f"FROM intake.job_requests WHERE dedupe_key = '{dedupe}'")
        if row and row.split("|")[1] == "started":
            break
        time.sleep(1)
    ok(bool(row), f"intake.job_requests row for {dedupe}: {row}")
    job, start_status, rb, requester, submitted_by = row.split("|")
    ok(start_status == "started" and submitted_by == "svc_github-integration", "start_status=started, "
       "submitted_by=svc_github-integration")
    d = subprocess.run([ENGINE, "exec", "ctl-temporal", "temporal", "workflow", "describe", "-n", "replicant-local",
                        "-w", job, "-o", "json"], capture_output=True, text=True, check=True).stdout
    info = json.loads(d)
    vi = (info.get("workflowExecutionInfo") or {}).get("versioningInfo") or {}
    ov = (vi.get("versioningOverride") or {})
    pinned = json.dumps(ov)
    ok("replicant-worker" in pinned and "PINNED" in pinned.upper(), f"workflow {job} type="
       f"{info['workflowExecutionInfo']['type']['name']} versioningOverride={pinned}")
    comment = None
    for _ in range(30):
        s, comments = call("GET", f"{GITEA}/api/v1/repos/{ORG}/{REPO}/issues/{issue['number']}/comments", headers=adm)
        comment = next((c for c in comments if job in c["body"]), None)
        if comment:
            break
        time.sleep(1)
    ok(comment is not None and comment["user"]["login"] == "replicant-bot",
       f"ack comment by {comment and comment['user']['login']}: {comment and comment['body'].splitlines()[0]}")
    # duplicate trigger (label removed and re-added) collapses onto the same job
    call("DELETE", f"{GITEA}/api/v1/repos/{ORG}/{REPO}/issues/{issue['number']}/labels/{lid}", headers=adm)
    call("POST", f"{GITEA}/api/v1/repos/{ORG}/{REPO}/issues/{issue['number']}/labels", {"labels": [lid]},
         {**adm, "Sudo": "alice"})
    time.sleep(4)
    n = psql(f"SELECT count(*) FROM intake.job_requests WHERE dedupe_key = '{dedupe}'")
    ok(n == "1", "re-labelling deduplicates to the same job (1 intake row)")
    # signed Gitea-shaped PR merged event for the job's branch -> pr.event Signal
    body = json.dumps({"action": "closed", "number": 1, "repository": repo,
                       "sender": {"id": 2, "login": "alice", "username": "alice"},
                       "pull_request": {"number": 1, "merged": True, "state": "closed",
                                        "head": {"ref": f"replicant/{job}", "sha": "a" * 40}, "base": {"ref": "main"}}}).encode()
    sig = hmac.new(secret("gitea_webhook_secret").encode(), body, hashlib.sha256).hexdigest()
    guid = str(uuid.uuid4())
    s, _ = call("POST", f"{GI}/webhooks/gitea", raw=body, headers={"X-Gitea-Event": "pull_request",
                                                                   "X-Gitea-Event-Type": "pull_request",
                                                                   "X-Gitea-Delivery": guid, "X-Gitea-Signature": sig})
    ok(s == 202, f"signed pull_request closed(merged) webhook accepted -> {s}")
    s, _ = call("POST", f"{GI}/webhooks/gitea", raw=body, headers={"X-Gitea-Event": "pull_request",
                                                                   "X-Gitea-Delivery": str(uuid.uuid4()),
                                                                   "X-Gitea-Signature": "0" * 64})
    ok(s == 401, f"bad signature rejected -> {s}")
    outcome = ""
    for _ in range(20):
        outcome = psql(f"SELECT state||'|'||coalesce(outcome,'') FROM github_app.inbound_deliveries WHERE "
                       f"delivery_guid = '{guid}'")
        if outcome.startswith(("done", "ignored", "dead")):
            break
        time.sleep(1)
    ok(outcome == "done|pr_event_signalled", f"delivery {guid}: {outcome}")
    if os.environ.get("STUB_WORKER", "1") == "1":
        st = ""
        for _ in range(20):
            st = json.loads(subprocess.run([ENGINE, "exec", "ctl-temporal", "temporal", "workflow", "describe", "-n",
                                            "replicant-local", "-w", job, "-o", "json"], capture_output=True, text=True,
                                           check=True).stdout)["workflowExecutionInfo"]["status"]
            if "COMPLETED" in st:
                break
            time.sleep(1)
        ok("COMPLETED" in st, f"stub ReplicantJob received pr.event pull_request_merged and completed ({st})")
    s, jwks = call("GET", f"{GRANTS}/.well-known/jwks.json")
    ok(s == 200 and jwks["keys"][0]["kid"] == "rg-local-1" and "d" not in jwks["keys"][0], "run-grants JWKS (public only)")
    s, rev = call("GET", f"{GRANTS}/grants/revoked")
    ok(s == 200 and {"generated_at", "job_ids", "run_ids"} <= set(rev), f"/grants/revoked: {len(rev['job_ids'])} jobs, "
       f"{len(rev['run_ids'])} runs")
    s, b = call("GET", f"{CPA}/internal/v1/tenants/payments/gateway-quota",
                headers={"Authorization": f"Bearer {secret('shim_token')}"})
    ok(s == 200 and b["daily_budget_usd"] == 10, f"gateway-quota for the shim -> {s} {b}")


if __name__ == "__main__":
    {"seed": seed, "run": run}[sys.argv[1]]()
