"""Memory snapshots (the selection pinned for a job), injection of a snapshot into a run, and a job's memory."""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text

from memory_service import core
from memory_service.db import Database, one, rows
from memory_service.store import ITEM_SQL, _item, _ok


# ================================================================================================ snapshots
async def create_snapshot(db: Database, s3: Any, req: dict[str, Any]) -> dict[str, Any]:
    tenant, job = req["tenant_id"], req["job_id"]
    repos = [r for r in (core.norm_repo(x) for x in req.get("repos") or []) if r]
    policy = (req.get("policy") or {}).get("ref") or "policy/memory-snapshot-default@1"
    if not re.match(r"^policy/[a-z][a-z0-9]*(-[a-z0-9]+)*(\.[a-z][a-z0-9-]{1,31})?@[1-9][0-9]*$", policy):
        policy = "policy/memory-snapshot-default@1"
    pdigest = (req.get("policy") or {}).get("digest") or core.sha256_digest(policy)
    async with db.tx(tenant) as c:
        prev = one(await c.execute(text("SELECT snapshot_id FROM memory.job_snapshots WHERE job_id = :j ORDER BY ordinal "
                                        "LIMIT 1"), {"j": job}))
        if prev:
            n = (await c.execute(text("SELECT item_count FROM memory.snapshots WHERE snapshot_id = :s"),
                                 {"s": prev["snapshot_id"]})).scalar()
            return {"memory_snapshot_id": prev["snapshot_id"], "entries": n or 0, "mode": "create", "empty": not n}
        res = rows(await c.execute(text(ITEM_SQL.format(extra="") + """
          WHERE e.state = 'active' AND (e.scope_level IN ('org', 'tenant')
             OR (e.scope_level IN ('repo', 'path') AND e.repo = ANY(CAST(:repos AS text[]))))
          ORDER BY e.entry_id"""), {"repos": repos}))
        items = [_item(r) for r in res]
        for i in items:
            i.base_score = core.score(i)
        items = sorted(items, key=lambda i: -i.base_score)[:400]
        items = core.precedence(items)
        refs = [i.ref for i in items]
        sid = core.snapshot_id(tenant, refs, policy)
        exists = (await c.execute(text("SELECT created_at FROM memory.snapshots WHERE snapshot_id = :s"),
                                  {"s": sid})).scalar()
        if exists is None:
            key = f"tenants/{tenant}/snapshots/{sid}.json"
            export = {"schema": "replicant.memory-snapshot-export/v1", "snapshot_id": sid, "tenant_id": tenant,
                      "policy_ref": policy, "renderer_version": core.RENDERER_VERSION,
                      "items": [{"ref": i.ref, "section": i.section, "scope": i.scope_label, "statement": i.statement,
                                 "precedence_rank": n + 1} for n, i in enumerate(items)]}
            sha = await s3.put("memory", key, json.dumps(export, sort_keys=True).encode())  # blob first, row second
            spec = core.sha256_digest(json.dumps({"repos": repos, "workflow_type": req.get("workflow_type")}, sort_keys=True))
            created = (await c.execute(text("""
              INSERT INTO memory.snapshots (snapshot_id, tenant_id, policy_ref, policy_digest, spec_digest, item_count,
                                            export_key, export_sha256, renderer_version, first_job_id)
              VALUES (:s, :t, :p, :pd, :sd, :n, :k, :sha, :rv, :j) RETURNING created_at"""),
                {"s": sid, "t": tenant, "p": policy, "pd": pdigest, "sd": spec, "n": len(items), "k": key, "sha": sha,
                 "rv": core.RENDERER_VERSION, "j": _ok("job", job)})).scalar_one()
            for n, i in enumerate(items):
                await c.execute(text("""
                  INSERT INTO memory.snapshot_items (snapshot_id, entry_id, version, tenant_id, section, precedence_rank,
                                                     scope_specificity, base_score, created_at)
                  VALUES (:s, :e, :v, :t, :sec, :r, :sp, :b, :at)"""),
                    {"s": sid, "e": i.entry_id, "v": i.version, "t": tenant, "sec": i.section, "r": n + 1,
                     "sp": core.LEVEL_RANK.get(i.scope_level, 0), "b": i.base_score, "at": created})
        if repos and _ok("job", job):
            await c.execute(text("""
              INSERT INTO memory.job_snapshots (job_id, ordinal, tenant_id, snapshot_id, reason, scope_tenant_id, repos,
                     repo, workflow_type, release_bundle_id, pull_as_of, requester_principal_id, idempotency_key)
              VALUES (:j, 1, :t, :s, 'create', :t, CAST(:repos AS text[]), :repo, :wt, :rb, now(), :req, :idem)
              ON CONFLICT DO NOTHING"""),
                {"j": job, "t": tenant, "s": sid, "repos": repos[:20], "repo": repos[0],
                 "wt": req.get("workflow_type") or "bugfix",
                 "rb": _ok("rb", req.get("release_bundle_id")) or "rb_" + "0" * 26,
                 "req": req.get("requester_principal_id") if re.match(r"^(usr_|svc_)", str(req.get(
                     "requester_principal_id") or "")) else "svc_replicant-worker", "idem": f"{job}:create"})
    return {"memory_snapshot_id": sid, "entries": len(items), "mode": "create", "empty": not items}


async def select_revoked(db: Database, tenant: str, snapshot: str) -> list[str]:
    """Refs of the snapshot whose entry is no longer active (revocation filter; nothing is ever added)."""
    async with db.tx(tenant) as c:
        res = rows(await c.execute(text("""
          SELECT i.entry_id || '@' || i.version AS ref FROM memory.snapshot_items i
            JOIN memory.entries e ON e.entry_id = i.entry_id
           WHERE i.snapshot_id = :s AND e.state <> 'active' ORDER BY i.precedence_rank"""), {"s": snapshot}))
    return [r["ref"] for r in res]


# ================================================================================================ injection
async def inject(db: Database, req: dict[str, Any]) -> dict[str, Any]:
    tenant, job, run, sid, role = req["tenant_id"], req["job_id"], req["run_id"], req["snapshot_id"], req["role"]
    cli = req.get("cli") if req.get("cli") in ("claude-code", "claude-agent-sdk", "codex") else "claude-code"
    record = role in core.ROLE_BUDGETS and _ok("run", run) and _ok("job", job)
    budget = core.ROLE_BUDGETS.get(role, core.ROLE_BUDGETS["implementer"])
    async with db.tx(tenant) as c:
        prior = one(await c.execute(text("SELECT * FROM memory.injections WHERE run_id = :r"), {"r": run})) if record else None
        if prior:  # retried activity: re-render the recorded refs in the recorded order
            refs = list(prior["injected_refs"])
            res = rows(await c.execute(text(ITEM_SQL.format(extra="").replace("v.version = e.current_version", "TRUE") + """
              WHERE (e.entry_id || '@' || v.version) = ANY(CAST(:refs AS text[]))"""), {"refs": refs}))
            by = {f"{r['entry_id']}@{r['version']}": _item(r) for r in res}
            sel = [by[x] for x in refs if x in by]
            return _doc(req, sid, sel, [], list(prior["cut_refs"]), budget, cli)
        res = rows(await c.execute(text(ITEM_SQL.format(extra=", CASE WHEN :q = '' THEN 0 ELSE ts_rank_cd(v.tsv, "
                                                          "websearch_to_tsquery('english', :q)) END AS rank")
                                        .replace("v.version = e.current_version", "v.version = i.version")
                                        .replace("FROM memory.entries e", "FROM memory.snapshot_items i JOIN memory.entries e ON e.entry_id = i.entry_id")
                                        + """
          WHERE i.snapshot_id = :s ORDER BY i.precedence_rank"""), {"s": sid, "q": (req.get("query") or "")[:2000]}))
        revoked = [f"{r['entry_id']}@{r['version']}" for r in res if r["state"] != "active"]
        items = []
        top = max([float(r["rank"] or 0) for r in res] + [0.0])
        for r in res:
            if r["state"] != "active":
                continue
            it = _item(r)
            it.rel = float(r["rank"] or 0) / top if top > 0 else 0.0
            items.append(it)
        sel, cut, used = core.pack(items, role, req.get("paths") or None, budget)
        doc = _doc(req, sid, sel, revoked, [i.ref for i in cut], budget, cli)
        if record:
            await c.execute(text("""
              INSERT INTO memory.injections (run_id, job_id, tenant_id, snapshot_id, role, cli, node_key, cycle,
                     injected_refs, cut_refs, token_count, budget_tokens, tokenizer, rendered_sha256, renderer_version,
                     artifact_key)
              VALUES (:r, :j, :t, :s, :role, :cli, :nk, :cy, CAST(:refs AS text[]), CAST(:cut AS text[]), :tok, :bt,
                      'chars/4', :sha, :rv, :ak) ON CONFLICT DO NOTHING"""),
                {"r": run, "j": job, "t": tenant, "s": sid, "role": role, "cli": cli,
                 "nk": re.sub(r"[^a-z0-9_]", "_", str(req.get("node_key") or "run"))[:41] or "run",
                 "cy": int(req.get("cycle") or 0), "refs": [i.ref for i in sel], "cut": [i.ref for i in cut],
                 "tok": min(doc["budget"]["used_tokens"], budget[0]), "bt": budget[0], "sha": doc["rendered_sha256"],
                 "rv": core.RENDERER_VERSION, "ak": (req.get("artifact_key") or f"tenants/{tenant}/jobs/{job}/runs/{run}/inputs/memory-injection.json")[:1024]})
            if sel:
                await c.execute(text("""UPDATE memory.entries SET injection_count = injection_count + 1,
                                        last_injected_at = now() WHERE entry_id = ANY(CAST(:ids AS text[]))"""),
                                {"ids": [i.entry_id for i in sel]})
            for ref in revoked:
                eid, v = ref.split("@")
                await c.execute(text("""INSERT INTO memory.usage_events (tenant_id, entry_id, version, job_id, run_id, event)
                                        VALUES (:t, :e, :v, :j, :r, 'revoked_midjob')"""),
                                {"t": tenant, "e": eid, "v": int(v), "j": job, "r": run})
    return doc


def _doc(req: dict[str, Any], sid: str, sel: list[core.Item], revoked: list[str], cut: list[str],
         budget: tuple[int, int], cli: str) -> dict[str, Any]:
    import hashlib
    rendered = core.render(sid, sel) if sel else ""
    used = core.tokens(rendered) if rendered else 0
    return {"schema": "replicant.memory-injection/v1", "job_id": req["job_id"], "run_id": req["run_id"],
            "tenant_id": req["tenant_id"], "memory_snapshot_id": sid,
            "role": req["role"] if req["role"] in core.ROLE_BUDGETS else "implementer", "cli": cli,
            "render_format": "replicant.memory-block/v1",
            "budget": {"budget_tokens": budget[0], "used_tokens": min(used, budget[0]), "tokenizer": "chars/4"},
            "items": [{"ref": i.ref, "section": i.section, "scope": i.scope_label, "precedence_rank": n + 1,
                       "tokens": core.tokens(core.line(i)), "curated": True} for n, i in enumerate(sel)],
            "cut": cut, "withheld": revoked, "rendered": rendered,
            "rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(), "renderer_version": core.RENDERER_VERSION,
            "summary": [{"ref": i.ref, "memory_type": i.memory_type, "scope": i.scope_label,
                         "statement": i.statement[:200]} for i in sel]}


async def job_memory(db: Database, tenant: str, job: str) -> dict[str, Any]:
    async with db.tx(tenant) as c:
        runs = rows(await c.execute(text("""SELECT run_id, role, node_key, cycle, snapshot_id, injected_refs, created_at
                                             FROM memory.injections WHERE job_id = :j ORDER BY created_at"""), {"j": job}))
        snap = (await c.execute(text("SELECT snapshot_id FROM memory.job_snapshots WHERE job_id = :j ORDER BY ordinal "
                                     "DESC LIMIT 1"), {"j": job})).scalar()
        refs = sorted({r for x in runs for r in x["injected_refs"]})
        ents = rows(await c.execute(text("""
          SELECT v.entry_id || '@' || v.version AS ref, e.memory_type, v.scope_level, v.repo, v.path_globs, v.title,
                 v.statement, e.state FROM memory.entry_versions v JOIN memory.entries e ON e.entry_id = v.entry_id
           WHERE (v.entry_id || '@' || v.version) = ANY(CAST(:refs AS text[]))"""), {"refs": refs})) if refs else []
    for e in ents:
        it = core.Item(entry_id="", version=1, memory_type=e["memory_type"], scope_level=e["scope_level"], repo=e["repo"],
                       statement="", path_globs=list(e["path_globs"] or []))
        e["scope"] = it.scope_label
        e["purged"] = e["statement"] is None
        for k in ("repo", "path_globs", "scope_level"):
            e.pop(k, None)
    return {"job_id": job, "memory_snapshot_id": snap or (runs[0]["snapshot_id"] if runs else None),
            "runs": [{"run_id": r["run_id"], "role": r["role"], "node_key": r["node_key"], "cycle": r["cycle"],
                      "refs": list(r["injected_refs"]), "at": r["created_at"].isoformat()} for r in runs],
            "entries": ents}
