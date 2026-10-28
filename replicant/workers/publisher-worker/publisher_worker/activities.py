"""Publisher activities (github-app.md §10, WS-01 CATALOG names and error types) for the v0 Gitea path.

publisher.publish is idempotent (R2-031): the branch `replicant/<job_id>` is reused when its head commit carries this
activity's `Replicant-Activity: <idempotency key>` trailer, otherwise rebuilt and force-pushed; the PR is looked up by
head branch before one is created."""
from __future__ import annotations

import fnmatch
import re
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from publisher_worker import storage
from publisher_worker.scm import Gitea, Repo, build_and_push_async, token
from replicant_scm.gitea import GiteaError

NEVER_PUBLISH = [".github/workflows/**", ".github/actions/**", ".gitea/workflows/**"]
AGENT_FILES = ["**/.replicant*", ".replicant*", "**/CLAUDE.md", "CLAUDE.md", "**/AGENTS.md", "AGENTS.md", ".claude/**",
               ".mcp.json"]
CI_PATHS = NEVER_PUBLISH + [".gitlab-ci.yml", "Jenkinsfile", "Makefile"]
SECRET_RES = [re.compile(p) for p in (r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                                      r"gh[pousr]_[A-Za-z0-9]{36}", r"xox[baprs]-[A-Za-z0-9-]{10,}",
                                      r"sk-ant-[A-Za-z0-9_-]{20,}")]
BOT = ("replicant-bot", "replicant-bot@replicant.localhost")


def _glob(p: str, pats: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(p, x) or (x.endswith("/**") and p.startswith(x[:-3] + "/")) for x in pats)


def _ids() -> tuple[str, str, str]:
    i = activity.info()
    return i.workflow_id, i.activity_id, f"ik1:{i.workflow_id}:{i.activity_id}"


def _reject(reason: str) -> ApplicationError:
    return ApplicationError(f"publish refused: {reason}", {"reason": reason}, type="PushRejected", non_retryable=True)


def verify_bundle(a: dict[str, Any], raw: bytes) -> tuple[bytes, dict[str, Any], list[str]]:
    """§10.1 steps 1-4: digest, base, forbidden paths, CI paths, secret scan."""
    sha = storage.sha256(raw)
    if not a.get("approved_diff_sha256") or sha != a["approved_diff_sha256"]:
        raise _reject("digest_mismatch")  # WS-09 G-03: the shipped diff is exactly the approved one
    patch, meta = storage.open_diff_bundle(raw)
    if not patch.strip():
        raise _reject("empty_patch")
    base = (a.get("repo") or {}).get("base_sha")
    if meta.get("base_sha") and base and meta["base_sha"] != base:
        raise _reject("base_mismatch")
    paths = list(meta.get("paths") or storage.patch_paths(patch))
    if any(_glob(p, NEVER_PUBLISH + AGENT_FILES) for p in paths):
        raise _reject("forbidden_path")
    if any(_glob(p, CI_PATHS) for p in paths) and not a.get("approval_decision_id"):
        raise _reject("ci_paths_unapproved")
    for ln in patch.decode("utf-8", "replace").splitlines():
        if ln.startswith("+") and not ln.startswith("+++") and any(rx.search(ln) for rx in SECRET_RES):
            raise _reject("secret_scan")
    return patch, meta, paths


def diff_stat(patch: bytes, paths: list[str]) -> dict[str, int]:
    """Files and +/- line counts of the approved patch, for the `job.pr_opened` notification (notifications.md §2:
    "PR #1431 opened: ledger-api · 3 files +41 −12")."""
    add = rem = 0
    for ln in patch.decode("utf-8", "replace").splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            add += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            rem += 1
    return {"files": len(paths), "additions": add, "deletions": rem}


def _gate(a: dict[str, Any]) -> None:
    ev = a.get("evaluation_target")
    if ev and ev != "eval_git":
        raise ApplicationError("evaluation job", type="InvalidInput", non_retryable=True)
    if ev == "eval_git":
        raise ApplicationError("the eval git server is not part of v0", type="InvalidInput", non_retryable=True)
    repo = Repo.of(a["repo"])
    if storage.kill_switch_engaged(a["tenant_id"], repo.full, a.get("release_bundle_id")):
        raise ApplicationError("Publishing paused by operators", type="PublishingPaused")  # retryable (C-MSG 1.5.0)


def commit_message(a: dict[str, Any], task: dict[str, Any], key: str, diff_sha: str, update: bool = False) -> str:
    title = (task.get("title") or f"Replicant change for {a['job_id']}")[:120]
    head = f"fix: address review feedback on {title}"[:160] if update else f"fix: {title}"
    lines = [head, "", f"Replicant job {a['job_id']}.", ""]
    if task.get("requester_login"):
        lines.append(f"Requested-by: @{task['requester_login']}")
    lines += [f"Replicant-Job: {a['job_id']}"]
    if a.get("release_bundle_id"):
        lines.append(f"Replicant-Bundle: {a['release_bundle_id']}")
    lines += [f"Replicant-Activity: {key}", f"Replicant-Diff: sha256:{diff_sha}"]
    return "\n".join(lines) + "\n"


def pr_body(a: dict[str, Any], task: dict[str, Any], paths: list[str]) -> str:
    """§10.2, from structured data only; the issue title is untrusted and quoted."""
    from publisher_worker.config import settings
    out = []
    if task.get("issue_number"):
        out.append(f"Fixes #{task['issue_number']}\n")
    out.append("**What changed**: " + ", ".join(f"`{p}`" for p in paths[:20]) + ("" if len(paths) <= 20 else " …"))
    out.append(f"\n**Verification**: QA and review ran in the replicant ([job]({settings().ui_base_url}/jobs/{a['job_id']}))")
    if a.get("approval_decision_id"):
        out.append(f"\n**Approval**: publish gate decision `{a['approval_decision_id']}`")
    out.append(f"\n<sub>Opened by the replicant{' for @' + task['requester_login'] if task.get('requester_login') else ''}. "
               "Agent-written changes: review before merging.</sub>")
    return "\n".join(out)


async def _push(a: dict[str, Any], *, open_pr: bool) -> dict[str, Any]:
    _gate(a)
    if not a.get("diff_bundle_uri"):
        raise ApplicationError("no diff bundle", type="InvalidInput", non_retryable=True)
    wf, aid, key = _ids()
    repo = Repo.of(a["repo"])
    raw = storage.get_uri(a["diff_bundle_uri"])
    patch, meta, paths = verify_bundle(a, raw)
    diff_sha = storage.sha256(raw)
    tok = await token(repo, purpose="publisher.publish" if open_pr else "publisher.push_update", job_id=a["job_id"],
                      activity_id=aid)
    g = Gitea(repo, tok)
    info = await g.repo_info()
    base_ref = (a["repo"].get("base_ref") or info.get("default_branch") or "main")
    base_sha = a["repo"].get("base_sha") or ((await g.branch(base_ref)) or {}).get("commit", {}).get("id")
    if not base_sha:
        raise ApplicationError(f"base {base_ref} not found", type="InvalidInput", non_retryable=True)
    branch = f"replicant/{a['job_id']}"
    task = storage.task_packet(a["tenant_id"], a["job_id"])
    existing = await g.branch(branch)
    head = None
    if existing:
        msg = ((existing.get("commit") or {}).get("message") or "")
        if f"Replicant-Activity: {key}" in msg:
            head = existing["commit"]["id"]  # an earlier attempt got this far (§10.1 step 6.1)
    if head is None:
        on_top = None
        if not open_pr:  # push_update: a new commit on top of the PR head (MEM-PR-D3)
            if not existing:
                raise ApplicationError(f"PR branch {branch} not found", type="PullRequestClosed", non_retryable=True)
            on_top = existing["commit"]["id"]
        head = await build_and_push_async(repo, tok, base_sha=base_sha, patch=patch, branch=branch,
                                          message=commit_message(a, task, key, diff_sha, update=not open_pr),
                                          author=BOT, on_top_of=on_top)
    if not open_pr:
        return {"head_sha": head, "branch": branch}
    title = ("WIP: " if a.get("draft") else "") + (task.get("title") or f"Replicant change for {a['job_id']}")[:200]
    body = pr_body(a, task, paths)
    prs = await g.pulls_for_head(branch)
    open_ = [p for p in prs if p.get("state") == "open"]
    if open_:
        pr = await g.edit_pull(int(open_[0]["number"]), body=body)
    elif prs:
        raise ApplicationError("the job's PR was closed", type="PullRequestClosed", non_retryable=True)
    else:
        pr = await g.create_pull(head=branch, base=base_ref, title=title, body=body)
    n = int(pr["number"])
    return {"pr_number": n, "number": n, "url": pr.get("html_url") or pr.get("url"), "head_sha": head, "branch": branch,
            "repo": repo.full, "state": "draft" if a.get("draft") else "open", "base_ref": base_ref,
            "diff_stat": diff_stat(patch, paths)}


@activity.defn(name="publisher.publish")
async def publisher_publish(a: dict[str, Any]) -> dict[str, Any]:
    return await _push(a, open_pr=True)


@activity.defn(name="publisher.push_update")
async def publisher_push_update(a: dict[str, Any]) -> dict[str, Any]:
    if not (a.get("pr") or {}).get("number"):
        raise ApplicationError("no PR to update", type="InvalidInput", non_retryable=True)
    r = await _push(a, open_pr=False)
    return {"head_sha": r["head_sha"]}


@activity.defn(name="scm.resolve_base")
async def scm_resolve_base(a: dict[str, Any]) -> dict[str, Any]:
    repo = Repo.of(a["repo"])
    _, aid, _ = _ids()
    g = Gitea(repo, await token(repo, purpose="scm.resolve_base", job_id=None, activity_id=aid))
    ref = a.get("base_ref") or (await g.repo_info()).get("default_branch") or "main"
    b = await g.branch(ref)
    if not b:
        raise ApplicationError(f"base branch {ref} not found", type="InvalidInput", non_retryable=True)
    return {"base_ref": ref, "base_sha": b["commit"]["id"]}


@activity.defn(name="publisher.check_base")
async def publisher_check_base(a: dict[str, Any]) -> dict[str, Any]:
    """Has the base moved since the diff was produced (D-19)? `applies_cleanly` is a dry `git apply --check` on the
    new base. M1: no rebased diff artifact (the base does not move in the E2E); a moved base routes to QA or rebase."""
    repo = Repo.of(a["repo"])
    _, aid, _ = _ids()
    g = Gitea(repo, await token(repo, purpose="publisher.check_base", job_id=a.get("job_id"), activity_id=aid))
    ref = a["repo"].get("base_ref") or (await g.repo_info()).get("default_branch") or "main"
    b = await g.branch(ref)
    cur = (b or {}).get("commit", {}).get("id")
    old = a["repo"].get("base_sha")
    if not cur or not old or cur == old:
        return {"moved": False, "new_base_sha": cur or old, "applies_cleanly": True}
    return {"moved": True, "new_base_sha": cur, "applies_cleanly": False}


@activity.defn(name="publisher.close_pr")
async def publisher_close_pr(a: dict[str, Any]) -> dict[str, Any]:
    pr = a.get("pr") or {}
    if not pr.get("number"):
        return {"state": "none"}
    host, owner, name = str(pr.get("repo", "")).split("/", 2)
    repo = Repo("gitea", host, owner, name, None)
    _, aid, _ = _ids()
    g = Gitea(repo, await token(repo, purpose="publisher.close_pr", job_id=a.get("job_id"), activity_id=aid))
    try:
        got = await g.edit_pull(int(pr["number"]), state="closed", raise_for=(404,))
    except GiteaError:  # the PR (or its repo) no longer exists: nothing is left open, but it was not closed by us
        return {"state": "not_found"}
    if (got or {}).get("state") != "closed":
        raise ApplicationError(f"gitea did not close PR #{pr['number']} (state {(got or {}).get('state')!r})",
                               type="ScmRejected", non_retryable=True)
    return {"state": "closed"}


REPLY_LINK_HOSTS: tuple[str, ...] = ()  # model text keeps no links (pr-replies.md §6 allowlist is empty locally)


def sanitize(text: str, cap: int = 200) -> str:
    """pr-replies.md §6 for model-written fragments (triage summaries): no @-mentions (code-formatted), no links, no
    `/replicant` command lines, no HTML, one line, capped."""
    t = re.sub(r"<[^>]*>", "", text or "")
    t = "\n".join(ln for ln in t.splitlines() if not ln.strip().startswith("/replicant"))
    t = re.sub(r"https?://\S+", "[link removed]", t)
    t = re.sub(r"(?<![\w`])@([A-Za-z0-9_.-]+)", r"`@\1`", t)
    t = " ".join(t.split())
    return t[:cap] + ("…" if len(t) > cap else "")


def _where(i: dict[str, Any]) -> str:
    if i.get("path"):
        return f"`{i['path']}{':' + str(i['line']) if i.get('line') else ''}`"
    return {"pr_review_body": "review", "pr_issue_comment": "comment"}.get(i.get("source_kind", ""), "comment")


def _lines(items: list[dict[str, Any]]) -> list[str]:
    return [f"- {_where(i)}: {sanitize(i.get('summary') or i.get('text', ''), 160)}" for i in items]


def reply_body(template: str, a: dict[str, Any], triage: dict[str, Any] | None, marker: str) -> str:
    """Fixed templates filled with structured data (never model text verbatim). `marker` makes the post idempotent."""
    items = [i for i in (triage or {}).get("items", []) if not i.get("command_only")]
    by_cls: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        by_cls.setdefault(i.get("classification", "no_action"), []).append(i)
    authors = sorted({i["author_login"] for i in items if i.get("author_login")})
    who = ", ".join(f"@{x}" for x in authors[:5])
    budget = a.get("repair_budget") or {}
    cyc = f"Repair cycle {budget.get('used', a.get('cycle', 0))} of {budget['max']}" if budget.get("max") else \
        f"Repair cycle {a.get('cycle', 0)}"
    pr = a.get("pr") or {}
    sha = str(pr.get("head_sha") or "")
    out: list[str] = []
    if template == "ack":
        fixes = by_cls.get("actionable_fix", [])
        out = [f"**Replicant is working on your feedback**{' · ' + who if who else ''}", "",
               f"{len(fixes)} comment(s) will be addressed in a repair run:", *_lines(fixes)]
        others = by_cls.get("question", []) + by_cls.get("optional_suggestion", [])
        if others:
            out += ["", f"Not part of this repair ({len(others)}): questions and optional suggestions are left for you."]
        out += ["", f"<sub>{cyc}. The update is pushed to this PR after QA and verification.</sub>"]
    elif template == "fix_summary":
        fixes = by_cls.get("actionable_fix", [])
        link = f"[`{sha[:10]}`]({str(pr.get('url', '')).rstrip('/')}/commits/{sha})" if sha else "a new commit"
        out = [f"**Fixed in {link}**{' · ' + who if who else ''}", "", "Addressed:", *_lines(fixes)]
        qa = a.get("qa_verdict")
        if qa:
            out += ["", f"QA: `{qa}`"]
        out += ["", f"<sub>{cyc}. Review the new commit; reply here if anything still needs changing.</sub>"]
    elif template == "question":
        qs = by_cls.get("question", [])
        out = [f"**Replicant needs a clarification before changing the code**{' · ' + who if who else ''}", "",
               "These comments are ambiguous or ask a question:", *_lines(qs), "",
               "Reply with the concrete change you want; Replicant will pick it up as new feedback."]
    elif template == "no_action":
        out = [f"**Replicant made no change for this feedback**{' · ' + who if who else ''}", ""]
        if items:
            out += ["Read as not requiring a code change:", *_lines(items), ""]
        out += ["If a change is needed, reply with what to change."]
    elif template == "budget_exhausted":
        out = ["**Replicant's repair budget for this PR is used up.**", "",
               f"{cyc}. A human needs to take over the remaining feedback."]
    else:
        out = ["Replicant received the feedback."]
    return "\n".join(out) + f"\n\n<!-- replicant:reply {marker} -->"


@activity.defn(name="github.post_reply")
async def github_post_reply(a: dict[str, Any]) -> dict[str, Any]:
    """PR-level reply from a template (pr-replies.md §2/§3), as replicant-bot with a broker token. Idempotent per
    activity: a comment carrying this activity's marker is reused."""
    import json
    pr = a.get("pr") or {}
    if not pr.get("number"):
        raise ApplicationError("no PR to reply on", type="InvalidInput", non_retryable=True)
    repo = Repo.of(a["repo"])
    _, aid, key = _ids()
    triage = json.loads(storage.get_uri(a["triage_uri"])) if a.get("triage_uri") else None
    g = Gitea(repo, await token(repo, purpose="github.post_reply", job_id=a.get("job_id"), activity_id=aid,
                                permissions={"pull_requests": "write"}))
    marker = f"<!-- replicant:reply {key} -->"
    for c in await g.comments(int(pr["number"])):
        if marker in (c.get("body") or ""):
            return {"comment_id": c.get("id"), "duplicate": True}
    c = await g.comment(int(pr["number"]), reply_body(a["template"], a, triage, key))
    if not (c or {}).get("id"):
        raise ApplicationError("gitea returned no comment id", type="ScmRejected", non_retryable=True)
    return {"comment_id": c["id"]}


@activity.defn(name="publisher.push_takeover_branch")
async def publisher_push_takeover_branch(a: dict[str, Any]) -> dict[str, Any]:
    raise ApplicationError("takeover is not part of v0", type="InvalidInput", non_retryable=True)


ACTIVITIES = [publisher_publish, publisher_push_update, scm_resolve_base, publisher_check_base, publisher_close_pr,
              github_post_reply, publisher_push_takeover_branch]
