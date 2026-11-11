"""Run inputs (WS-03 agents.*, WS-04 context.*): briefing, run plan, inputs manifest and gateway run policy.

M1 has no memory-service, context-mcp or Langfuse, so the worker composes the briefing and prompts itself from the
JobRequest and the bundle's v0 prompt texts (local_bundle.PROMPTS). Every object is written once (`If-None-Match: *`);
a retried activity that finds the object reuses it and its digest, so the RunSpec decision fields never change
(C-RUNNER 2.4.1 / XD-FIX-R1-014).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from temporalio.exceptions import ApplicationError

from replicant_worker import local_bundle, tiers
from replicant_worker.activities import bundle as bundles
from replicant_worker.activities import s3
from replicant_worker.activities.base import http, implements, info_ids, parse_lid, retryable_http
from replicant_worker.activities.config import read_secret, settings

RUN_AUDIENCES = {  # RUNNER-02 per role; ai-gateway + runner-sandbox + repo-mirror always (agentd needs them)
    "implementer": ["ai-gateway", "repo-mirror", "runner-sandbox"],
    "repair": ["ai-gateway", "repo-mirror", "runner-sandbox"],
    "planner": ["ai-gateway", "repo-mirror", "runner-sandbox"],
    "reviewer": ["ai-gateway", "repo-mirror", "runner-sandbox"],
    "qa": ["ai-gateway", "repo-mirror", "runner-sandbox"],
}
BASE_SCOPES = ["gateway:invoke", "repo:fetch", "credentials:aws"]


def run_prefix(tenant: str, job: str, run_id: str) -> str:
    return f"tenants/{tenant}/jobs/{job}/runs/{run_id}/"


def _reuse_or_put(r: s3.Ref, body: bytes, content_type: str) -> dict[str, Any]:
    """Write-once; if the object exists already (an earlier attempt), return the existing object's digest."""
    if s3.exists(r):
        sha, size = s3.sha256_of(r)
        return {"key": r.key, "uri": r.uri, "sha256": sha, "size_bytes": size, "created": False}
    try:
        return s3.put_once(r, body, content_type)
    except ApplicationError as e:
        if e.type != "ObjectConflict":
            raise
        sha, size = s3.sha256_of(r)
        return {"key": r.key, "uri": r.uri, "sha256": sha, "size_bytes": size, "created": False}


def _j(doc: Any) -> bytes:
    return json.dumps(doc, indent=2, sort_keys=True).encode()


# ------------------------------------------------------------------------------------------------ briefing
def _task(req: dict[str, Any]) -> dict[str, Any]:
    intent = req.get("intent") or {}
    src = req.get("source") or {}
    t0 = (req.get("targets") or [{}])[0]
    return {"title": intent.get("title", "")[:256], "body": intent.get("text", "")[:60000],
            "source": src.get("channel", "api"), "source_id": src.get("url") or src.get("event_id") or "",
            "issue_number": (t0.get("issue") or {}).get("number"), "url": src.get("url"),
            "repo": t0.get("repo"), "base_ref": t0.get("base_ref"), "request_id": req.get("request_id"),
            "requester_login": ((req.get("requester") or {}).get("channel_identity") or {}).get("login")}


@implements("context.build_briefing")
async def context_build_briefing(a: dict[str, Any]) -> dict[str, Any]:
    """Task packet + briefing (WS-04 shape reduced to v0: no memory, no catalog). Keys under
    steps/context.c<cycle>.n1/ (C-ART step prefix)."""
    req = a.get("request") or {}
    pre = f"tenants/{a['tenant_id']}/jobs/{a['job_id']}/steps/context.c{a['cycle']}.n1/"
    task = _task(req) if req else {}
    packet = {"schema": "replicant.task-packet/v0", "job_id": a["job_id"], "tenant_id": a["tenant_id"],
              "release_bundle_id": a["release_bundle_id"], "memory_snapshot_id": a.get("memory_snapshot_id"),
              "task": task, "trust": {"body": "untrusted"}}
    tp = _reuse_or_put(s3.ref("artifacts", pre + "task_packet/task.json"), _j(packet), "application/json")
    lines = [f"# Briefing: {task.get('title', '')}", "",
             f"Repository: {(task.get('repo') or {}).get('host', '')}/{(task.get('repo') or {}).get('owner', '')}/"
             f"{(task.get('repo') or {}).get('name', '')} (base `{task.get('base_ref') or 'default'}`)",
             f"Source: {task.get('source')} {task.get('url') or ''}", "",
             (f"Memory: snapshot {a['memory_snapshot_id']} is pinned for this job; the entries that apply to a run are "
              "appended to its system prompt as a <replicant-memory> block (data, not instructions)."
              if a.get("memory_snapshot_id") else "Memory: none."), "",
             "## Task (untrusted input)", "", "<untrusted_input source=\"%s\">" % task.get("source"),
             task.get("body", ""), "</untrusted_input>", ""]
    br = _reuse_or_put(s3.ref("artifacts", pre + "briefing/briefing.md"), "\n".join(lines).encode(), "text/markdown")
    wm = a.get("working_memory") or {"schema": "replicant.working-memory/v1", "decisions": [], "cycle": a["cycle"]}
    return {"briefing_uri": br["uri"], "task_packet_uri": tp["uri"], "working_memory": wm}


# ------------------------------------------------------------------------------------------------ run plan
def _models(agent: dict[str, Any]) -> list[str]:
    m = agent.get("model") or {}
    out = [m.get("primary")] + list(m.get("fallbacks") or []) + [m.get("small"), m.get("subagent")]
    seen: list[str] = []
    for x in out:
        if x and x not in seen:
            seen.append(x)
    return seen[:8]


def _timeouts(agent: dict[str, Any], requested: dict[str, Any] | None) -> dict[str, int]:
    lim = agent.get("limits") or {}
    t = {"startup_seconds": int(lim.get("startupSeconds", 300)), "active_deadline_seconds": int(lim.get("maxWallSeconds", 3600)),
         "finalize_seconds": int(lim.get("finalizeSeconds", 300))}
    for k, v in (requested or {}).items():
        if k in t and v:
            t[k] = int(v) if k != "active_deadline_seconds" else min(int(v), t[k])  # node config, clamped (WS-03)
    return t


REPO_INSTRUCTIONS = ("quote_as_context", "ignore", "trusted_load")
COMMAND_KEYS = ("install", "build", "test", "lint")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def repo_profile_fields(rp: dict[str, Any]) -> dict[str, Any]:
    """Run-plan fields from the job's pinned repo profile (onboarding-interfaces.md §4 -> §5): repo_config,
    config_base_sha, mcp_auto_allow. environment.commands is added by plan_document."""
    mode = rp.get("repo_config") if rp.get("repo_config") in ("use", "ignore") else "ignore"
    sha = rp.get("config_base_sha") or rp.get("sha")
    allow = []
    for m in rp.get("mcp_auto_allow") or []:
        if isinstance(m, dict) and _MCP_NAME.match(str(m.get("name") or "")) and m.get("command"):
            allow.append({"name": str(m["name"]), "command": str(m["command"])[:256],
                          "args": [str(x)[:512] for x in (m.get("args") or [])][:32]})
    return {"repo_config": mode, "config_base_sha": sha if isinstance(sha, str) and _SHA.match(sha) else None,
            "mcp_auto_allow": allow[:16]}


def plan_document(a: dict[str, Any], agent_doc: dict[str, Any], env_doc: dict[str, Any], *, budget: float,
                  timeouts: dict[str, int], audiences: list[str], scopes: list[str], network_profile: str,
                  base_sha: str | None, agent_spec: dict[str, Any] | None = None,
                  repo_instructions: str = "quote_as_context") -> dict[str, Any]:
    """`agent_spec`: the agent spec with the run's model tier applied (default: the definition's). With a pinned
    `repo_profile` in `a` the plan also carries environment.commands, repo_config, config_base_sha and
    mcp_auto_allow, and the coding roles bootstrap with the repo's own install command."""
    spec, env = agent_spec or agent_doc["spec"], env_doc["spec"]
    role = a["role"]
    texts = local_bundle.PROMPTS.get((agent_doc.get("metadata") or {}).get("name", "")) \
        or local_bundle.PROMPTS.get(role, local_bundle.PROMPTS["implementer"])  # MEM: agent-specific prompts (scanner)

    def ptxt(t: str) -> dict[str, Any]:
        return {"text": t, "sha256": hashlib.sha256(t.encode()).hexdigest()}
    eff: dict[str, Any] = {"budget_usd": budget, "max_turns": int(spec["limits"].get("maxTurns", 100)),
                           "active_deadline_seconds": timeouts["active_deadline_seconds"],
                           "finalize_seconds": timeouts["finalize_seconds"],
                           "steering_live": bool((spec.get("steering") or {}).get("live")) and bool(a.get("steering_requested")),
                           "models": _models(spec), "audiences": audiences, "scopes": scopes,
                           "network_profile": network_profile, "deterministic": bool(a.get("deterministic"))}
    if a.get("deterministic") and a.get("received_at"):
        eff["git_identity_date"] = str(a["received_at"])
    boot = env.get("bootstrap") or {}
    steps = [{k: s[k] for k in ("name", "run", "timeoutSeconds") if k in s} for s in boot.get("steps", [])] \
        if role in (boot.get("forRoles") or [role]) else []
    if role == "qa":  # QA installs and builds itself; only `seed-*` steps run ahead of it (design §8c)
        steps = [s for s in steps if str(s.get("name", "")).startswith("seed-")]
    rp = a.get("repo_profile") or None
    cmds = (rp or {}).get("commands") or {}
    if rp and role != "qa" and cmds.get("install") and role in (boot.get("forRoles") or [role]):
        steps = [s for s in steps if str(s.get("name", "")).startswith("seed-")] + \
            [{"name": "repo-install", "run": str(cmds["install"])[:4096], "timeoutSeconds": 900}]
    doc = {"schema": "replicant.run-plan/v1", "run_id": a["run_id"], "role": role, "agent": spec, "effective": eff,
           "prompts": {"role": ptxt(texts["role"]), "constraints": ptxt(local_bundle.CONSTRAINTS),
                       "task_template": ptxt(texts["task"]), "finalize": ptxt(local_bundle.FINALIZE)},
           "workspace": {"repo_path": "/workspace/repo", **({"base_sha": base_sha} if base_sha else {}),
                         "branch_name": f"replicant/{a['job_id']}"},
           "environment": {"codebase_type": env.get("codebaseType"), "bootstrap_steps": steps, "env": dict(env.get("env") or {}),
                           "package_mirrors": list(env.get("packageMirrors") or []),
                           "caches": [{k: c[k] for k in ("name", "path", "kind", "env") if k in c} for c in env.get("caches", [])],
                           "services": []},
           "repo_instructions": repo_instructions if repo_instructions in REPO_INSTRUCTIONS else "quote_as_context"}
    if rp:  # design §8c (onboarding-interfaces.md §5); absent fields mean today's behaviour
        doc["environment"]["commands"] = {k: (str(cmds[k])[:4096] if cmds.get(k) else None) for k in COMMAND_KEYS}
        doc.update(repo_profile_fields(rp))
    return doc


def _repo_instructions(b: bundles.Bundle, repo: dict[str, Any] | None) -> str:
    """The codebase binding's repoInstructions for the job's repo (the bindings policy default when no binding
    matches); quote_as_context without a bindings policy (v0's local bundle)."""
    pol = b.policies().get("codebase_bindings") or {}
    binding = b.binding_for(repo) if repo else None
    v = (binding or {}).get("repoInstructions") or (pol.get("default") or {}).get("repoInstructions")
    return v if v in REPO_INSTRUCTIONS else "quote_as_context"


@implements("agents.build_run_plan")
async def agents_build_run_plan(a: dict[str, Any]) -> dict[str, Any]:
    """WS-03 agent-definition.md §3. Writes inputs/run-plan.json once and returns the effective per-run values.
    `tier_facts` (sent by workflows with the model-tiers-v1 patch) selects the run's model tier (replicant_worker.tiers)
    from the bundle's model_tiers / model_tier_selection policies; `repo_profile` and `repo` (repo-profile-env-v1)
    add the repo's commands and configuration mode and the binding's repoInstructions."""
    b = bundles.load(a["release_bundle_id"])
    agent_pin, agent_doc = b.agent(a["agent_ref"])
    env_pin, env_doc = b.environment(a["environment_ref"])
    spec = agent_doc["spec"]
    if a["role"] not in spec.get("roles", []) and not (a["role"] == "repair" and "implementer" in spec.get("roles", [])):
        raise ApplicationError(f"agent {a['agent_ref']} does not declare role {a['role']}", type="InvalidInput",
                               non_retryable=True)
    tier = None
    if a.get("tier_facts") is not None:
        tier = tiers.resolve(b.policies(), role=a["role"], agent_name=agent_pin["name"],
                             workflow_type=str(a.get("workflow_type") or ""), facts=a["tier_facts"])
    run_spec = tiers.apply(spec, tier["params"]) if tier else spec
    t = _timeouts(spec, a.get("timeouts"))
    audiences = RUN_AUDIENCES.get(a["role"], RUN_AUDIENCES["implementer"])
    scopes = list(BASE_SCOPES) + (["inbox:read"] if (spec.get("steering") or {}).get("live") else [])
    net = a.get("network_profile") or (env_doc["spec"].get("networkProfile") or {}).get(spec.get("workloadClass"), "coding-default")
    max_cost = float(spec["limits"].get("maxCostUsd", 10))
    # effective.budget_usd must equal RunSpec.limits.max_cost_usd, which the workflow computes after this call as
    # min(allocation, plan max_cost_usd, remaining job budget): allocation == min(plan, job cap) in M1, so it is
    # min(plan max_cost, job cap).
    budget = round(min(max_cost, float(a.get("job_cap_usd") or max_cost)), 2)
    doc = plan_document(a, agent_doc, env_doc, budget=budget, timeouts=t, audiences=audiences, scopes=scopes,
                        network_profile=net, base_sha=a.get("base_sha"), agent_spec=run_spec,
                        repo_instructions=_repo_instructions(b, a.get("repo")))
    r = s3.ref("artifacts", run_prefix(a["tenant_id"], a["job_id"], a["run_id"]) + "inputs/run-plan.json")
    put = _reuse_or_put(r, _j(doc), "application/json")
    out = {"agent_ref": agent_pin, "environment_ref": env_pin,
           "run_plan": {"name": "run_plan", "key": r.key, "sha256": put["sha256"], "size_bytes": put["size_bytes"]},
           "audiences": audiences, "scopes": scopes, "network_profile": net,
           "resource_profile": (env_doc["spec"].get("profileByRole") or {}).get(a["role"], a.get("resource_profile") or "default"),
           "timeouts": t, "max_cost_usd": max_cost, "steering_live": doc["effective"]["steering_live"],
           "models": doc["effective"]["models"], "max_turns": doc["effective"]["max_turns"], "max_concurrency": 4}
    if tier:  # recorded by the workflow (run.model_selected) and in the run's observations
        p = (doc["agent"].get("model") or {})
        out["tier"] = {"tier": tier["tier"], "role": tier["role"], "escalated": tier["escalated"],
                       "model": p.get("primary"), "reasoning_effort": (p.get("parameters") or {}).get("reasoningEffort"),
                       "thinking_tokens": (p.get("parameters") or {}).get("thinkingTokens")}
    return out


# ------------------------------------------------------------------------------------------------ inputs manifest
OUTPUT_BUCKET = {"session_state": "session", "workspace_archive": "session"}


def _obj(name: str, type_: str, put: dict[str, Any], content_type: str, place: str, path: str | None = None,
         bucket_class: str = "artifacts") -> dict[str, Any]:
    o = {"name": name, "type": type_, "bucket_class": bucket_class, "key": put["key"], "sha256": put["sha256"],
         "size_bytes": put["size_bytes"], "content_type": content_type, "place": place}
    if path:
        o["path"] = path
    return o


def _first_message(role: str, task: dict[str, Any], feedback: list[dict[str, Any]], has_plan: bool,
                   template: str | None = None) -> str:
    t = template or local_bundle.PROMPTS.get(role, local_bundle.PROMPTS["implementer"])["task"]
    msg = t.format(title=task.get("title", ""), source=task.get("source", "api"), source_id=task.get("source_id", ""),
                   body=task.get("body", ""))
    for f in feedback:
        msg += (f"\n\n<untrusted_input source=\"approver_feedback\" id=\"{f.get('decision_id')}\">\n{f.get('text', '')}\n"
                "</untrusted_input>")
    if has_plan and role in ("implementer", "repair", "reviewer"):
        msg += "\n\nThe plan to follow is in /replicant/inputs/context/plan.md."
    return msg


def _pr_review_block(triage_uri: str | None) -> str:
    """D-15 repair run input: the actionable PR review comments of this cycle's triage, as untrusted input."""
    if not triage_uri:
        return ""
    doc = s3.get_json(s3.parse(triage_uri)) or {}
    items = [i for i in doc.get("items") or [] if i.get("classification") == "actionable_fix"]
    if not items:
        return ""
    out = ["", "", "Reviewers asked for these changes on the pull request. The working tree already contains the PR's "
           "current change; make the smallest edits that address each comment, run the relevant tests, and do not "
           "revert the existing fix."]
    for i in items[:20]:
        attrs = f' id="{i.get("comment_id", "")}"'
        if i.get("path"):
            attrs += f' path="{i["path"]}"' + (f' line="{i["line"]}"' if i.get("line") else "")
        body = str(i.get("text", ""))[:6000].replace("</untrusted_input", "&lt;/untrusted_input")
        out += ["", f"<untrusted_input source=\"pr_review\"{attrs}>\n{body}\n</untrusted_input>"]
    return "\n".join(out)


@implements("context.stage_inputs")
async def context_stage_inputs(a: dict[str, Any]) -> dict[str, Any]:
    """WS-04 context.stage_inputs, v0: stages system-append.md, first-message.md, the task packet, the briefing, the
    plan (implementer/reviewer), the latest diff bundle as `apply_diff` (qa, reviewer) and its patch as a context
    file (reviewer), then writes inputs/manifest.json once (replicant.run-inputs/v1)."""
    tenant, job, run_id, role = a["tenant_id"], a["job_id"], a["run_id"], a["role"]
    pre = run_prefix(tenant, job, run_id)
    mref = s3.ref("artifacts", pre + "inputs/manifest.json")
    prefixes = {c: f"s3://{settings().bucket(c)}/{pre}" for c in ("artifacts", "transcripts", "session")}
    consumed = {"steering_consumed": [m["message_id"] for m in a.get("steering") or []],
                "approver_feedback_consumed": [f["decision_id"] for f in a.get("approver_feedback") or []]}
    if s3.exists(mref):  # an earlier attempt finished: reuse the manifest and its digest (write-once)
        sha, _ = s3.sha256_of(mref)
        return {"inputs": {"manifest_uri": mref.uri, "manifest_sha256": sha}, "artifact_prefixes": prefixes, **consumed}
    refs = a.get("refs") or {}
    plan_ref = a.get("run_plan") or {}
    b = bundles.load(a["release_bundle_id"])
    if not plan_ref.get("key"):
        raise ApplicationError("run_plan entry missing", type="InvalidInput", non_retryable=True)
    plan_doc = json.loads(s3.get_bytes(s3.ref("artifacts", plan_ref["key"])))
    task: dict[str, Any] = {}
    if refs.get("task_packet_uri"):
        task = (s3.get_json(s3.parse(refs["task_packet_uri"])) or {}).get("task") or {}
    objects: list[dict[str, Any]] = [{"name": "run_plan", "type": "run_inputs", "bucket_class": "artifacts",
                                      "key": plan_ref["key"], "sha256": plan_ref["sha256"],
                                      "size_bytes": int(plan_ref["size_bytes"]), "content_type": "application/json",
                                      "place": "run_plan"}]
    system = "\n\n".join([plan_doc["prompts"]["role"]["text"], plan_doc["prompts"]["constraints"]["text"]])
    objects.append(_obj("system_append", "agent_prompt", _reuse_or_put(s3.ref("artifacts", pre + "inputs/system-append.md"),
                        system.encode(), "text/markdown"), "text/markdown", "system_append"))
    has_plan = bool(refs.get("plan_uri")) and role in ("implementer", "repair", "reviewer")
    fm = _first_message(role, task, list(a.get("approver_feedback") or []), has_plan,
                        ((plan_doc.get("prompts") or {}).get("task_template") or {}).get("text"))
    pr_repair = role == "repair" and bool(refs.get("feedback_triage_uri"))
    if pr_repair:
        fm += _pr_review_block(refs.get("feedback_triage_uri"))
    objects.append(_obj("first_message", "agent_prompt", _reuse_or_put(s3.ref("artifacts", pre + "inputs/first-message.md"),
                        fm.encode(), "text/markdown"), "text/markdown", "first_message"))
    if refs.get("task_packet_uri"):
        body = s3.get_bytes(s3.parse(refs["task_packet_uri"]))
        objects.append(_obj("task_packet", "task_packet", _reuse_or_put(s3.ref("artifacts", pre + "inputs/task_packet.json"),
                            body, "application/json"), "application/json", "context_file", "task_packet.json"))
    if refs.get("briefing_uri"):
        body = s3.get_bytes(s3.parse(refs["briefing_uri"]))
        objects.append(_obj("briefing", "briefing", _reuse_or_put(s3.ref("artifacts", pre + "inputs/briefing.md"),
                            body, "text/markdown"), "text/markdown", "context_file", "briefing.md"))
    if has_plan:
        body = s3.get_bytes(s3.parse(refs["plan_uri"]))
        objects.append(_obj("plan", "plan", _reuse_or_put(s3.ref("artifacts", pre + "inputs/plan.md"), body,
                            "text/markdown"), "text/markdown", "context_file", "plan.md"))
    diff = refs.get("latest_diff_bundle_uri")
    if diff and (role in ("qa", "reviewer") or pr_repair):  # a PR repair starts from the published change
        body = s3.get_bytes(s3.parse(diff))
        put = _reuse_or_put(s3.ref("artifacts", pre + "inputs/changes.tar.zst"), body,
                            "application/vnd.replicant.diff-bundle.v1+tar+zstd")
        objects.append(_obj("diff_bundle", "diff_bundle", put, "application/vnd.replicant.diff-bundle.v1+tar+zstd",
                            "apply_diff"))
        if role == "reviewer":
            from replicant_worker.activities.core import read_diff_bundle
            patch = read_diff_bundle(diff)["patch"]
            objects.append(_obj("changes_patch", "diff_bundle", _reuse_or_put(s3.ref("artifacts", pre + "inputs/changes.patch"),
                                patch, "text/x-diff"), "text/x-diff", "context_file", "changes.patch"))
    # MEM: select + render this run's memory block (memory-service records memory.injections); staged as
    # inputs/memory-injection.json, place system_append_memory (the entrypoint appends `.rendered`)
    from replicant_worker.activities import memory as memact
    mem_refs: list[str] = []
    inj = await memact.stage_injection(a, pre + "inputs/memory-injection.json",
                                       f"{task.get('title', '')}\n{str(task.get('body', ''))[:1500]}")
    if inj and inj.get("rendered"):
        doc = {k: v for k, v in inj.items() if k != "summary"}
        put = _reuse_or_put(s3.ref("artifacts", pre + "inputs/memory-injection.json"), _j(doc), "application/json")
        objects.append(_obj("memory_injection", "memory_injection", put, "application/json", "system_append_memory"))
        mem_refs = [i["ref"] for i in doc.get("items") or []]
    manifest = {"schema": "replicant.run-inputs/v1", "tenant_id": tenant, "job_id": job, "run_id": run_id, "role": role,
                "release_bundle_id": a["release_bundle_id"], "created_at": _created_at(a),
                "agent_definition": a["agent_definition"] if a.get("agent_definition") else _pinned_from_plan(b, a, plan_doc, "agent"),
                "environment_definition": a["environment_definition"] if a.get("environment_definition")
                else _pinned_from_plan(b, a, plan_doc, "environment"),
                "objects": objects}
    if a.get("memory_snapshot_id"):
        manifest["memory_snapshot_id"] = a["memory_snapshot_id"]
    manifest["memory_entries"] = mem_refs
    put = _reuse_or_put(mref, _j(manifest), "application/json")
    return {"inputs": {"manifest_uri": mref.uri, "manifest_sha256": put["sha256"]}, "artifact_prefixes": prefixes,
            **consumed}


def _created_at(a: dict[str, Any]) -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _pinned_from_plan(b: bundles.Bundle, a: dict[str, Any], plan_doc: dict[str, Any], kind: str) -> dict[str, Any]:
    """The manifest's pinned agent/environment: the bundle documents whose spec equals the plan's (the plan holds the
    verified spec verbatim). The workflow input carries only names through `extra`, so match by content."""
    from replicant_worker.activities import jcs
    def unmodelled(spec: dict[str, Any]) -> str:  # the run's model tier may have replaced spec.model
        return jcs.digest({k: v for k, v in spec.items() if k != "model"})
    want = unmodelled(plan_doc["agent"]) if kind == "agent" else None  # compares two specs, not a registry digest
    for ref, doc in b.docs.items():
        if not ref.startswith(f"{kind}/"):
            continue
        if kind == "agent" and unmodelled(doc["spec"]) == want:
            return b.pinned(ref)
        if kind == "environment" and doc["spec"].get("codebaseType") == plan_doc["environment"].get("codebase_type"):
            return b.pinned(ref)
    raise ApplicationError(f"no {kind} in the bundle matches the run plan", type="NotInBundle", non_retryable=True)


# ------------------------------------------------------------------------------------------------ gateway run policy
async def put_run_policy(run_id: str, policy: dict[str, Any]) -> dict[str, Any]:
    s = settings()
    tok = read_secret(s.gateway_token_file)
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    r = await http().put(f"{s.gateway_url}/internal/v1/runs/{run_id}/policy", json=policy, headers=headers)
    if r.status_code == 200:
        return {"policy_registered": True}
    if r.status_code == 409:
        # identical run_id already registered with a different body: only expires_at differs on a retry (it is
        # computed from the attempt's clock), which the shim keeps from the first registration
        return {"policy_registered": True, "existing": True}
    if r.status_code in (400, 403, 422):
        raise ApplicationError(f"gateway rejected the run policy: HTTP {r.status_code}", type="InvalidInput",
                               non_retryable=True)
    raise retryable_http("gateway PUT run policy", r)


def stub_for(a: dict[str, Any]) -> dict[str, Any] | None:
    s = settings()
    if a.get("stub_selection"):
        return dict(a["stub_selection"])
    _, aid = info_ids()
    lid = parse_lid(aid)
    node, cycle, ordinal = (lid[0], lid[1], lid[2]) if lid else (a.get("node_key", "run"), a.get("cycle", 0), 1)
    if s.cassette_set:
        return {"root": "tests", "set": s.cassette_set, "key": f"{node}.c{cycle}.n{ordinal}"}
    if s.script_set:
        return {"root": "scripts", "set": s.script_set,
                "key": s.script_key.format(node_key=node, cycle=cycle, ordinal=ordinal, role=a.get("role", ""),
                                           agent=(a.get("plan") or {}).get("agent_ref", {}).get("name", ""))}
    return None


def policy_body(*, tenant_id: str, job_id: str, role: str, release_bundle_id: str, models: list[str], budget_usd: float,
                max_concurrency: int, lifetime_seconds: int) -> dict[str, Any]:
    """gateway-interface RunPolicy; expires_at >= startup + active + finalize + 1 h, rounded up to the hour."""
    exp = (datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds + 2 * 3600)).replace(minute=0, second=0, microsecond=0)
    return {"tenant_id": tenant_id, "job_id": job_id, "role": role, "release_bundle_id": release_bundle_id,
            "models": list(models)[:8], "budget_usd": float(budget_usd), "max_concurrency": int(max_concurrency or 4),
            "expires_at": exp.isoformat().replace("+00:00", "Z")}


async def put_job_budget(tenant_id: str, job_id: str, job_budget_usd: float) -> None:
    """PUT /internal/v1/jobs/{job_id}/budget (idempotent per job): without it local shims cap a job at $5 (GATEWAY-D8)."""
    s = settings()
    tok = read_secret(s.gateway_token_file)
    headers = {"Idempotency-Key": f'"job-budget:{job_id}"', **({"Authorization": f"Bearer {tok}"} if tok else {})}
    r = await http().put(f"{s.gateway_url}/internal/v1/jobs/{job_id}/budget", headers=headers,
                         json={"tenant_id": tenant_id, "job_budget_usd": float(job_budget_usd)})
    if r.status_code >= 500:
        raise retryable_http("gateway PUT job budget", r)
    if r.status_code not in (200, 201, 204, 409):
        from temporalio import activity
        activity.logger.warning("gateway job budget not registered", extra={"status": r.status_code})


@implements("agents.stage_inputs")
async def agents_stage_inputs(a: dict[str, Any]) -> dict[str, Any]:
    """Registers the gateway run policy (PUT /internal/v1/runs/{run_id}/policy). No S3 writes (XD-FIX-R1-067)."""
    plan = a.get("plan") or {}
    t = plan.get("timeouts") or {}
    life = sum(int(t.get(k, 0)) for k in ("startup_seconds", "active_deadline_seconds", "finalize_seconds"))
    policy = policy_body(tenant_id=a["tenant_id"], job_id=a["job_id"], role=a["role"],
                         release_bundle_id=a["release_bundle_id"], models=list(plan.get("models") or []),
                         budget_usd=float(a["budget_usd"]), max_concurrency=int(plan.get("max_concurrency") or 4),
                         lifetime_seconds=life)
    if a.get("job_cap_usd"):
        await put_job_budget(a["tenant_id"], a["job_id"], float(a["job_cap_usd"]))
    if plan.get("budget_account"):
        policy["budget_account"] = plan["budget_account"]
    stub = stub_for(a) if settings().local else None
    if stub:
        policy["stub"] = stub
    return {**await put_run_policy(a["run_id"], policy), "stub": stub}
