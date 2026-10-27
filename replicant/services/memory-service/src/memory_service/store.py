"""SQL operations on schema `memory` (the single writer). State changes always go through `_transition`, which writes
`state_transitions` first: the `entries_state_guard` trigger refuses a state change without it."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from replicant_ids import ulid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from memory_service import core
from memory_service.auth import AdminPrincipal, HttpError
from memory_service.db import Database, jdump, one, rows

log = logging.getLogger(__name__)
SVC = "svc_memory-service"
LIVE = ("candidate", "active")
EXTRACTOR_DEFAULT = "memory-extraction@1"
ORIGIN_BY_KIND = {"pr_review_comment": "pr_feedback", "pr_review_body": "pr_feedback", "pr_issue_comment": "pr_feedback",
                  "replicant_command": "pr_feedback", "gate_comment": "pr_feedback", "review_finding": "negative_mining",
                  "qa_failure": "negative_mining", "agent_proposal": "agent_proposal", "import": "import",
                  "curator": "curator", "repair_rejection": "pr_feedback", "job_outcome": "negative_mining"}
HUMAN_KINDS = ("pr_review_comment", "pr_review_body", "pr_issue_comment", "replicant_command")
SOURCE_PREFIX = {"pr_review_comment": "gh-review-comment", "pr_review_body": "gh-review", "pr_issue_comment":
                 "gh-issue-comment", "replicant_command": "gh-issue-comment", "gate_comment": "gate",
                 "review_finding": "review", "qa_failure": "qa", "agent_proposal": "agent", "import": "scan",
                 "job_outcome": "outcome", "repair_rejection": "agent", "curator": "agent"}
TRANSITIONS = {"approve": (("candidate",), "active"), "reject": (("candidate",), "rejected"),
               "disable": (("active",), "disabled"), "deprecate": (("active", "disabled"), "deprecated"),
               "restore": (("disabled", "deprecated", "quarantined"), "active"),
               "quarantine": (("candidate", "active", "disabled", "deprecated"), "quarantined")}
ID_RE = {"job": re.compile(r"^job_[0-9a-hjkmnp-tv-z]{26}$"), "run": re.compile(r"^run_[0-9a-hjkmnp-tv-z]{26}$"),
         "usr": re.compile(r"^usr_[0-9a-hjkmnp-tv-z]{26}$"), "rb": re.compile(r"^rb_[0-9a-hjkmnp-tv-z]{26}$")}


def _ok(kind: str, v: Any) -> Any:
    return v if isinstance(v, str) and ID_RE[kind].match(v) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_:./-]", "-", s)[:200]


# ================================================================================================ read helpers
ITEM_SQL = """
SELECT e.entry_id, v.version, e.memory_type, v.scope_level, v.repo, v.statement, v.title, v.path_globs, v.roles,
       e.enforced, e.confidence::float AS confidence, e.created_at, e.last_confirmed_at, e.state{extra}
  FROM memory.entries e JOIN memory.entry_versions v ON v.entry_id = e.entry_id AND v.version = e.current_version
"""


def _item(r: dict[str, Any]) -> core.Item:
    return core.Item(entry_id=r["entry_id"], version=r["version"], memory_type=r["memory_type"],
                     scope_level=r["scope_level"], repo=r["repo"], statement=r["statement"] or "", title=r.get("title"),
                     path_globs=list(r.get("path_globs") or []), roles=list(r.get("roles") or []),
                     enforced=bool(r.get("enforced")), confidence=float(r.get("confidence") or 0.5),
                     created_at=r.get("created_at"), last_confirmed_at=r.get("last_confirmed_at"))


async def _emit(conn: AsyncConnection, ev: dict[str, Any]) -> str:
    """audit.emit inside a SAVEPOINT (control-plane-api's pattern); a failure is logged and an aud_ minted."""
    full = {"occurred_at": _now().isoformat(), "source_service": "memory-service", "channel": "ui",
            "outcome": "success", **ev}
    try:
        async with conn.begin_nested():
            aid = (await conn.execute(text("SELECT audit.emit(CAST(:e AS jsonb))"), {"e": jdump(full)})).scalar_one()
            log.info(json.dumps({"log_type": "audit", "audit_event_id": aid, "event_type": ev.get("event_type")}))
            return aid
    except Exception as e:  # noqa: BLE001
        log.warning("audit.emit failed (%s: %s)", type(e).__name__, str(e).splitlines()[0][:200])
    return ulid("aud")


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


# ================================================================================================ write path
async def write_candidates(db: Database, settings: Any, env: dict[str, Any], *, judge: Any = None) -> dict[str, Any]:
    tenant, job = env["tenant_id"], env["job_id"]
    prod = env.get("producer") or {}
    extractor = prod.get("extraction_prompt") or EXTRACTOR_DEFAULT
    out = {"written": 0, "merged": 0, "conflicts": 0, "blocked": 0, "duplicates_skipped": 0, "entries": []}
    seen: dict[str, int] = {}
    if any(not ((p.get("proposed_scope") or {}).get("repo") or (p.get("source") or {}).get("repo"))
           for p in env.get("proposals") or []):  # repo-less proposals (agent runs) take the job's repo
        env = {**env, "_job_repo": (await _job_binding(db, tenant, job)).get("repo")}
    async with db.tx(tenant) as c:
        for p in env.get("proposals") or []:
            src = dict(p.get("source") or {})
            kind = src.get("kind") or "agent_proposal"
            base_key = src.get("source_key") or f"{SOURCE_PREFIX.get(kind, 'agent')}:{job}:{len(seen)}"
            n = seen.get(base_key, 0)
            seen[base_key] = n + 1
            skey = _key(base_key if n == 0 else f"{base_key}:{n}")
            if one(await c.execute(text("SELECT 1 AS x FROM memory.feedback_items WHERE tenant_id = :t AND source_key = :k "
                                        "AND extractor = :x"), {"t": tenant, "k": skey, "x": extractor})):
                out["duplicates_skipped"] += 1
                continue
            r = await _one_candidate(c, settings, tenant, job, p, src, kind, skey, extractor, env, judge)
            out[{"candidate_created": "written", "evidence_merged": "merged", "conflict_opened": "conflicts"}.get(
                r["decision"], "blocked")] += 1
            if r["decision"] == "conflict_opened":
                out["written"] += 1
            if r.get("entry_id"):
                out["entries"].append({"entry_id": r["entry_id"], "decision": r["decision"]})
        for nc in env.get("non_candidates") or []:
            src = nc.get("source") or {}
            skey = _key(src.get("source_key") or f"{SOURCE_PREFIX.get(src.get('kind'), 'agent')}:{job}:nc")
            dec = nc.get("decision") if nc.get("decision") in ("not_generalizable", "opted_out") else "not_generalizable"
            await _ledger(c, tenant, skey, extractor, job, src.get("text") or skey, dec, None,
                          {"reason": (nc.get("reason") or "")[:500], "kind": src.get("kind")})
    return out


async def _ledger(c: AsyncConnection, tenant: str, skey: str, extractor: str, job: str, body: str, decision: str,
                  entry_id: str | None, detail: dict[str, Any]) -> None:
    await c.execute(text("""
      INSERT INTO memory.feedback_items (tenant_id, source_key, extractor, job_id, source_digest, decision, entry_id, detail)
      VALUES (:t, :k, :x, :j, :d, :dec, :e, CAST(:det AS jsonb)) ON CONFLICT DO NOTHING"""),
        {"t": tenant, "k": skey, "x": extractor, "j": job, "d": core.sha256_digest(body), "dec": decision, "e": entry_id,
         "det": jdump(detail)})


async def _one_candidate(c: AsyncConnection, settings: Any, tenant: str, job: str, p: dict[str, Any],
                         src: dict[str, Any], kind: str, skey: str, extractor: str, env: dict[str, Any],
                         judge: Any) -> dict[str, Any]:
    stype = p.get("memory_type") or "rule"
    scope = p.get("proposed_scope") or {}
    repo = core.norm_repo(scope.get("repo") or src.get("repo")) or (env.get("_job_repo") if scope.get("level") != "tenant" else None)
    globs = [g for g in scope.get("path_globs") or [] if isinstance(g, str)][:20]
    level = "path" if (globs and repo) else ("repo" if repo else "tenant")
    statement = core.redact(str(p.get("statement") or ""), 4000).strip()
    detail = {"kind": kind, "repo": repo, "text": core.redact(str(src.get("text") or statement), 600)}
    body = str(src.get("text") or statement)

    def blocked(dec: str, why: Any) -> dict[str, Any]:
        return {"decision": dec, "why": why}
    if stype == "episode" or len(statement) < 10:
        await _ledger(c, tenant, skey, extractor, job, body, "not_generalizable", None, {**detail, "why": "shape"})
        return blocked("not_generalizable", "shape")
    if stype == "preference" and level == "tenant":
        stype = "rule"
    if kind in HUMAN_KINDS and src.get("author_repo_permission") not in ("admin", "maintain", "write"):
        await _ledger(c, tenant, skey, extractor, job, body, "blocked_author", None, detail)
        return blocked("blocked_author", src.get("author_repo_permission"))
    if kind in HUMAN_KINDS and not _ok("usr", src.get("author_principal_id")):
        await _ledger(c, tenant, skey, extractor, job, body, "blocked_identity", None, detail)
        return blocked("blocked_identity", "unlinked")
    findings = core.safety_findings(" ".join(str(p.get(k) or "") for k in ("title", "statement", "rationale")))
    if findings:
        await _ledger(c, tenant, skey, extractor, job, body, "blocked_safety", None, {**detail, "rules": findings})
        return blocked("blocked_safety", findings)
    digest = core.content_digest(stype, level, repo, globs, statement)
    prov = _prov(src, kind, skey, env, job, p)
    # dedupe 1: exact content digest
    dup = one(await c.execute(text("""
      SELECT e.entry_id, e.current_version FROM memory.entries e JOIN memory.entry_versions v
        ON v.entry_id = e.entry_id AND v.version = e.current_version
       WHERE e.tenant_id = :t AND e.state IN ('candidate', 'active') AND v.content_digest = :d LIMIT 1"""),
        {"t": tenant, "d": digest}))
    sim = None
    if not dup:  # dedupe 2: trigram similarity on the same type and scope
        sim = one(await c.execute(text("""
          SELECT e.entry_id, e.current_version, v.statement, similarity(v.statement, :s) AS sim
            FROM memory.entries e JOIN memory.entry_versions v ON v.entry_id = e.entry_id AND v.version = e.current_version
           WHERE e.tenant_id = :t AND e.state IN ('candidate', 'active') AND e.memory_type = :ty
             AND e.scope_level = :lvl AND e.repo IS NOT DISTINCT FROM :repo AND similarity(v.statement, :s) >= 0.35
           ORDER BY sim DESC LIMIT 1"""), {"t": tenant, "s": statement, "ty": stype, "lvl": level, "repo": repo}))
        if sim and float(sim["sim"]) >= 0.6:
            dup = sim
        elif sim and judge is not None and await judge(tenant=tenant, job_id=_ok("job", job), a=sim["statement"],
                                                       b=statement):
            dup = sim
    if dup:
        ins = (await c.execute(text(_PROV_INSERT + " ON CONFLICT (entry_id, independence_key) WHERE withdrawn_at IS NULL "
                                    "DO NOTHING RETURNING provenance_id"),
                               {**prov, "e": dup["entry_id"], "t": tenant, "v": dup["current_version"]})).first()
        if ins:
            await c.execute(text("""UPDATE memory.entries SET evidence_count = evidence_count + 1,
                independent_evidence_count = independent_evidence_count + 1, confidence_alpha = confidence_alpha + 1,
                last_confirmed_at = now() WHERE entry_id = :e"""), {"e": dup["entry_id"]})
        await _ledger(c, tenant, skey, extractor, job, body, "evidence_merged", dup["entry_id"],
                      {**detail, "similarity": float(sim["sim"]) if sim else 1.0})
        return {"decision": "evidence_merged", "entry_id": dup["entry_id"]}
    subject = p.get("subject_key") if isinstance(p.get("subject_key"), str) and core.SUBJECT_RE.match(p["subject_key"]) else None
    origin = env.get("origin") or ORIGIN_BY_KIND.get(kind, "agent_proposal")
    eid = ulid("mem")
    await c.execute(text("""
      INSERT INTO memory.entries (entry_id, tenant_id, memory_type, origin, state, scope_level, repo, subject_key,
                                  created_by, evidence_count, independent_evidence_count, distinct_author_count,
                                  distinct_repo_count, candidate_expires_at)
      VALUES (:e, :t, :ty, :o, 'candidate', :lvl, :repo, :sk, :by, 1, 1, :da, :dr, now() + interval '90 days')"""),
        {"e": eid, "t": tenant, "ty": stype, "o": origin, "lvl": level, "repo": repo, "sk": subject, "by": SVC,
         "da": 1 if prov["author"] else 0, "dr": 1 if repo else 0})
    await _insert_version(c, eid, 1, tenant, stype, level, repo, globs, subject, p, statement, digest, "create", SVC)
    await c.execute(text(_PROV_INSERT), {**prov, "e": eid, "t": tenant, "v": 1})
    decision = "candidate_created"
    if subject:
        other = one(await c.execute(text("""
          SELECT e.entry_id, e.current_version FROM memory.entries e WHERE e.tenant_id = :t AND e.subject_key = :sk
             AND e.state IN ('candidate', 'active') AND e.entry_id <> :e AND e.repo IS NOT DISTINCT FROM :repo LIMIT 1"""),
            {"t": tenant, "sk": subject, "e": eid, "repo": repo}))
        if other:
            a, b = sorted([(eid, 1), (other["entry_id"], other["current_version"])])
            await c.execute(text("""
              INSERT INTO memory.conflicts (conflict_id, tenant_id, entry_a, version_a, entry_b, version_b, kind,
                                            detected_by, detector_detail, state)
              VALUES (:id, :t, :a, :va, :b, :vb, 'contradiction', 'subject_key', CAST(:d AS jsonb), 'open')
              ON CONFLICT DO NOTHING"""),
                {"id": ulid("mcfl"), "t": tenant, "a": a[0], "va": a[1], "b": b[0], "vb": b[1],
                 "d": jdump({"subject_key": subject})})
            decision = "conflict_opened"
    await _ledger(c, tenant, skey, extractor, job, body, decision, eid, detail)
    return {"decision": decision, "entry_id": eid}


_PROV_INSERT = """
INSERT INTO memory.provenance (entry_id, tenant_id, supports_version, source_kind, source_url, repo, repo_id, pr_number,
       comment_id, review_id, comment_digest, source_excerpt, author_principal_id, author_github_user_id,
       author_repo_permission, author_association, job_id, run_id, cycle, delivery_id, release_bundle_id,
       extraction_prompt_name, extraction_prompt_version, extraction_model, generalization, independence_key, observed_at)
VALUES (:e, :t, :v, :kind, :url, :repo, :repo_id, :pr, :cid, :rid, :cdig, :excerpt, :author, :ghid, :perm, :assoc,
        :job, :run, :cycle, :delivery, :rb, :pname, :pver, :model, CAST(:gen AS jsonb), :ikey, :obs)"""


def _prov(src: dict[str, Any], kind: str, skey: str, env: dict[str, Any], job: str, p: dict[str, Any]) -> dict[str, Any]:
    prod = env.get("producer") or {}
    pname, _, pver = (prod.get("extraction_prompt") or "").partition("@")
    url = src.get("url") or src.get("source_url")
    excerpt = src.get("text") or p.get("statement") or ""
    def _int(v: Any) -> int | None:
        try:
            return int(v) if v is not None and int(v) >= 1 else None
        except (TypeError, ValueError):
            return None
    assoc = src.get("author_association")
    obs = src.get("observed_at")
    return {"kind": kind, "url": url if isinstance(url, str) and re.match(r"^https?://[a-z0-9.:-]+/", url) else None,
            "repo": core.norm_repo(src.get("repo")), "repo_id": (str(src["repo_id"])[:64] if src.get("repo_id") else None),
            "pr": _int(src.get("pr_number")), "cid": _int(src.get("comment_id")), "rid": _int(src.get("review_id")),
            "cdig": core.sha256_digest(excerpt) if excerpt else None, "excerpt": core.redact(excerpt, 2000) or None,
            "author": _ok("usr", src.get("author_principal_id")), "ghid": _int(src.get("author_github_user_id")),
            "perm": src.get("author_repo_permission") if src.get("author_repo_permission") in (
                "admin", "maintain", "write", "triage", "read", "none") else None,
            "assoc": assoc if assoc in ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "FIRST_TIMER",
                                        "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", "NONE") else None,
            "job": _ok("job", src.get("job_id") or job), "run": _ok("run", src.get("run_id") or prod.get("run_id")),
            "cycle": _int(src.get("cycle")), "delivery": (str(src["delivery_id"])[:64] if src.get("delivery_id") else None),
            "rb": _ok("rb", prod.get("release_bundle_id")), "pname": pname[:200] or None,
            "pver": int(pver) if pver.isdigit() and int(pver) >= 1 else None, "model": (prod.get("extraction_model") or None),
            "gen": jdump(p.get("generalization")) if isinstance(p.get("generalization"), dict) else None,
            "ikey": core.sha256_digest(skey), "obs": obs if isinstance(obs, str) and obs else _now()}


async def _insert_version(c: AsyncConnection, eid: str, ver: int, tenant: str, stype: str, level: str, repo: str | None,
                          globs: list[str], subject: str | None, p: dict[str, Any], statement: str, digest: str,
                          change: str, by: str, note: str | None = None) -> None:
    scope = p.get("proposed_scope") or {}
    roles = [r for r in scope.get("roles") or [] if r in ("planner", "implementer", "qa", "reviewer", "repair")]
    ex = p.get("examples") if isinstance(p.get("examples"), list) else None
    await c.execute(text("""
      INSERT INTO memory.entry_versions (entry_id, version, tenant_id, scope_level, repo, path_globs, languages, roles,
             subject_key, title, statement, rationale, structured_schema, examples, token_estimate, content_digest,
             change_kind, change_note, created_by)
      VALUES (:e, :v, :t, :lvl, :repo, CAST(:globs AS text[]), CAST(:langs AS text[]), CAST(:roles AS text[]), :sk,
              :title, :st, :ra, :ss, CAST(:ex AS jsonb), :tok, :d, :ck, :note, :by)"""),
        {"e": eid, "v": ver, "t": tenant, "lvl": level, "repo": repo, "globs": globs if level == "path" else [],
         "langs": [str(x)[:32] for x in (scope.get("languages") or [])][:10], "roles": roles, "sk": subject,
         "title": (core.redact(str(p.get("title")), 120) if p.get("title") else None),
         "st": statement[:4000], "ra": (core.redact(str(p.get("rationale")), 1000) if p.get("rationale") else None),
         "ss": f"replicant.memory.{stype}/v1", "ex": jdump(ex[:4]) if ex else None, "tok": core.tokens(statement),
         "d": digest, "ck": change, "note": note, "by": by})


# ================================================================================================ recurrence
async def _job_binding(db: Database, tenant: str, job: str) -> dict[str, Any]:
    """repo / workflow_type / release_bundle_id of a job, from its snapshot binding (finalize inputs carry no repo)."""
    async with db.tx(tenant) as c:
        return one(await c.execute(text("""SELECT repo, workflow_type, release_bundle_id FROM memory.job_snapshots
            WHERE job_id = :j ORDER BY ordinal LIMIT 1"""), {"j": job})) or {}


async def findings(db: Database, settings: Any, req: dict[str, Any], judge: Any = None) -> dict[str, Any]:
    """Reviewer findings and QA failures of one job. A negative candidate is proposed only when a similar finding
    (trigram >= 0.6) was recorded for another job on the same repo (DEVIATIONS MEM-D4)."""
    tenant, job = req["tenant_id"], req["job_id"]
    repo = core.norm_repo(req.get("repo")) or (await _job_binding(db, tenant, job)).get("repo")
    out = {"recorded": 0, "recurring": 0, "result": None}
    proposals = []
    async with db.tx(tenant) as c:
        for i, f in enumerate((req.get("items") or [])[:50]):
            kind = f.get("kind") if f.get("kind") in ("review_finding", "qa_failure") else "review_finding"
            pre = SOURCE_PREFIX[kind]
            txt = core.redact(re.sub(r"\s+", " ", str(f.get("text") or "")).strip(), 600)
            if len(txt) < 12 or not repo:
                continue
            skey = _key(f"{pre}:{job}:{f.get('key') or i}")
            prev = one(await c.execute(text("""
              SELECT source_key, job_id, detail->>'text' AS t FROM memory.feedback_items
               WHERE tenant_id = :t AND source_key LIKE :pre AND job_id <> :j AND detail->>'repo' = :repo
                 AND similarity(detail->>'text', :x) >= 0.6 ORDER BY similarity(detail->>'text', :x) DESC LIMIT 1"""),
                {"t": tenant, "pre": pre + ":%", "j": job, "repo": repo, "x": txt}))
            if prev:
                out["recurring"] += 1
                who = "the reviewer agent" if kind == "review_finding" else "QA"
                proposals.append({"memory_type": "negative", "proposed_scope": {"level": "repo", "repo": repo},
                                  "statement": f"Recurring finding from {who} in more than one job: {txt}",
                                  "rationale": f"Seen in {prev['job_id']} and {job}.",
                                  "source": {"kind": kind, "source_key": skey + ":recur", "text": txt, "repo": repo,
                                             "job_id": job},
                                  "generalization": {"prescriptive": 0.7, "pr_independence": 0.8, "recurrence": 1.0,
                                                     "counterfactual_examples": []}})
            await _ledger(c, tenant, skey, "recurrence@1", job, txt, "not_generalizable", None,
                          {"kind": kind, "repo": repo, "text": txt, "severity": f.get("severity"),
                           "recurs_with": prev["job_id"] if prev else None})
            out["recorded"] += 1
    if proposals:
        out["result"] = await write_candidates(db, settings, {"tenant_id": tenant, "job_id": job,
                                                              "producer": {"kind": "negative_miner",
                                                                           "extraction_prompt": "recurrence@1"},
                                                              "proposals": proposals}, judge=judge)
    return out


async def record_outcome(db: Database, req: dict[str, Any]) -> dict[str, Any]:
    tenant, job = req["tenant_id"], req["job_id"]
    st = req.get("terminal_status")
    if st not in ("succeeded", "failed", "cancelled", "rejected", "expired", "taken_over"):
        st = "failed"
    if not (req.get("repo") and req.get("workflow_type") and req.get("release_bundle_id")):
        b = await _job_binding(db, tenant, job)
        req = {**req, "repo": req.get("repo") or b.get("repo"), "workflow_type": req.get("workflow_type")
               or b.get("workflow_type"), "release_bundle_id": req.get("release_bundle_id") or b.get("release_bundle_id")}
    async with db.tx(tenant) as c:
        await c.execute(text("""
          INSERT INTO memory.job_outcomes (job_id, tenant_id, repo, workflow_type, release_bundle_id, terminal_status,
                 pr_merged, review_rounds, changes_requested_reviews, human_comment_count, repeated_comment_count,
                 first_pass_qa, first_review_approved, memory_snapshot_ids)
          VALUES (:j, :t, :repo, :wt, :rb, :st, :merged, :rr, :cr, :hc, :rc, :fq, :fr, CAST(:snaps AS text[]))
          ON CONFLICT (job_id) DO UPDATE SET terminal_status = EXCLUDED.terminal_status, pr_merged = EXCLUDED.pr_merged,
                 review_rounds = EXCLUDED.review_rounds, changes_requested_reviews = EXCLUDED.changes_requested_reviews,
                 human_comment_count = EXCLUDED.human_comment_count, repeated_comment_count = EXCLUDED.repeated_comment_count,
                 first_pass_qa = EXCLUDED.first_pass_qa, first_review_approved = EXCLUDED.first_review_approved,
                 memory_snapshot_ids = EXCLUDED.memory_snapshot_ids, recorded_at = now(),
                 revision = memory.job_outcomes.revision + 1"""),
            {"j": job, "t": tenant, "repo": core.norm_repo(req.get("repo")) or str(req.get("repo") or "unknown"),
             "wt": req.get("workflow_type") or "bugfix", "rb": _ok("rb", req.get("release_bundle_id")) or "rb_" + "0" * 26,
             "st": st, "merged": req.get("pr_merged"), "rr": req.get("review_rounds"),
             "cr": req.get("changes_requested_reviews"), "hc": req.get("human_comment_count"),
             "rc": req.get("repeated_comment_count"), "fq": req.get("first_pass_qa"),
             "fr": req.get("first_review_approved"),
             "snaps": [s for s in req.get("memory_snapshot_ids") or [] if str(s).startswith("msnap_")]})
    return {"recorded": True}


# ================================================================================================ admin: reads
LIST_SQL = """
SELECT e.entry_id, e.tenant_id, e.memory_type, e.origin, e.state, e.state_seq, e.scope_level, e.repo, e.subject_key,
       e.current_version, e.confidence::float AS confidence, e.evidence_count, e.injection_count, e.created_at,
       e.state_changed_at, e.last_injected_at, v.title, v.statement, v.rationale, v.path_globs,
       (SELECT p.source_kind FROM memory.provenance p WHERE p.entry_id = e.entry_id ORDER BY p.provenance_id LIMIT 1) AS source_kind,
       (SELECT count(*) FROM memory.conflicts k WHERE k.state = 'open' AND (k.entry_a = e.entry_id OR k.entry_b = e.entry_id)) AS open_conflicts
  FROM memory.entries e JOIN memory.entry_versions v ON v.entry_id = e.entry_id AND v.version = e.current_version
"""


async def list_entries(db: Database, q: dict[str, Any]) -> list[dict[str, Any]]:
    where, params = ["TRUE"], {}
    for k, col in (("state", "e.state"), ("memory_type", "e.memory_type"), ("tenant_id", "e.tenant_id"),
                   ("origin", "e.origin"), ("scope_level", "e.scope_level")):
        if q.get(k):
            vals = str(q[k]).split(",")
            where.append(f"{col} = ANY(CAST(:{k} AS text[]))")
            params[k] = vals
    if q.get("repo"):
        where.append("e.repo = :repo")
        params["repo"] = core.norm_repo(q["repo"]) or q["repo"]
    if q.get("q"):
        where.append("(v.tsv @@ websearch_to_tsquery('english', :q) OR v.statement ILIKE :ql)")
        params["q"], params["ql"] = q["q"], f"%{q['q']}%"
    if q.get("job_id"):
        where.append("EXISTS (SELECT 1 FROM memory.provenance p WHERE p.entry_id = e.entry_id AND p.job_id = :job)")
        params["job"] = q["job_id"]
    params["lim"] = min(int(q.get("limit") or 200), 500)
    async with db.tx(admin=True) as c:
        res = rows(await c.execute(text(LIST_SQL + " WHERE " + " AND ".join(where)
                                        + " ORDER BY e.created_at DESC LIMIT :lim"), params))
    return [_jsonable(r) for r in res]


async def get_entry(db: Database, eid: str) -> dict[str, Any]:
    async with db.tx(admin=True) as c:
        e = one(await c.execute(text(LIST_SQL + " WHERE e.entry_id = :e"), {"e": eid}))
        if not e:
            raise HttpError(404, "entry_not_found", eid)
        versions = rows(await c.execute(text("""SELECT version, scope_level, repo, path_globs, subject_key, title, statement,
            rationale, change_kind, change_note, created_at, created_by, purged_at FROM memory.entry_versions
            WHERE entry_id = :e ORDER BY version DESC"""), {"e": eid}))
        prov = rows(await c.execute(text("""SELECT provenance_id, supports_version, source_kind, source_url, repo, pr_number,
            comment_id, review_id, source_excerpt, author_principal_id, author_repo_permission, job_id, run_id,
            extraction_prompt_name, extraction_prompt_version, extraction_model, generalization, observed_at, withdrawn_at,
            withdrawn_reason FROM memory.provenance WHERE entry_id = :e ORDER BY provenance_id"""), {"e": eid}))
        trans = rows(await c.execute(text("""SELECT from_state, to_state, entry_version, at, actor_kind, actor_principal_id,
            reason_code, reason_text FROM memory.state_transitions WHERE entry_id = :e ORDER BY transition_id"""), {"e": eid}))
        jobs = rows(await c.execute(text("""SELECT job_id, run_id, role, node_key, created_at, snapshot_id
            FROM memory.injections WHERE EXISTS (SELECT 1 FROM unnest(injected_refs) r WHERE r LIKE :p)
            ORDER BY created_at DESC LIMIT 100"""), {"p": eid + "@%"}))
        conflicts = rows(await c.execute(text("""SELECT conflict_id, entry_a, entry_b, kind, state, resolution, detected_at
            FROM memory.conflicts WHERE entry_a = :e OR entry_b = :e ORDER BY detected_at DESC"""), {"e": eid}))
    return _jsonable({**e, "versions": versions, "provenance": prov, "transitions": trans, "jobs": jobs,
                      "conflicts": conflicts})


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "is_finite"):  # Decimal
        return float(v)
    return v


# ================================================================================================ admin: mutations
async def _action(c: AsyncConnection, admin: AdminPrincipal, tenant: str, action: str, target: dict[str, Any],
                  idem: str, body: Any, event_type: str = "memory.admin_action") -> tuple[str, bool]:
    """Curator action (D-25), idempotent on (admin, Idempotency-Key). Returns (action_id, replayed)."""
    prior = (await c.execute(text("SELECT action_id FROM memory.curator_actions WHERE admin_principal_id = :a AND "
                                  "idempotency_key = :k"), {"a": admin.principal_id, "k": idem})).scalar()
    if prior:
        return prior, True
    aid = await _emit(c, {"event_type": event_type, "tenant_id": tenant, "actor_principal_id": admin.principal_id,
                          "details": {"action": action, **_flat(target)}})
    act = ulid("mact")
    await c.execute(text("""INSERT INTO memory.curator_actions (action_id, tenant_id, admin_principal_id, action, target,
        request_digest, idempotency_key, okta_groups_checked, audit_event_id)
        VALUES (:id, :t, :a, :act, CAST(:tg AS jsonb), :d, :k, CAST(:g AS text[]), :aud)"""),
        {"id": act, "t": tenant, "a": admin.principal_id, "act": action, "tg": jdump(target),
         "d": core.sha256_digest(jdump(body)), "k": idem, "g": [g for g in admin.groups if g.startswith("replicant-admins")]
         or list(admin.groups[:1]), "aud": aid})
    return act, False


def _flat(d: dict[str, Any]) -> dict[str, Any]:
    """audit.emit details: flat snake_case keys, scalars or arrays of scalars, strings <= 256 chars."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, (int, bool)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:256]
        elif isinstance(v, list) and all(isinstance(x, (str, int)) for x in v):
            out[k] = [x[:256] if isinstance(x, str) else x for x in v[:50]]
        else:
            out[k] = jdump(v)[:256]
    return out


async def _transition(c: AsyncConnection, e: dict[str, Any], to: str, admin: AdminPrincipal, act: str, reason: str,
                      text_: str | None = None, quarantine_id: str | None = None) -> None:
    await c.execute(text("""INSERT INTO memory.state_transitions (entry_id, tenant_id, from_state, to_state, entry_version,
        actor_kind, actor_principal_id, curator_action_id, quarantine_id, reason_code, reason_text)
        VALUES (:e, :t, :f, :to, :v, 'admin', :a, :act, :q, :rc, :rt)"""),
        {"e": e["entry_id"], "t": e["tenant_id"], "f": e["state"], "to": to, "v": e["current_version"],
         "a": admin.principal_id, "act": act, "q": quarantine_id, "rc": reason, "rt": (text_ or None) and text_[:2000]})
    await c.execute(text("""UPDATE memory.entries SET state = :to, state_seq = state_seq + 1,
        quarantine_id = CASE WHEN :to = 'quarantined' THEN :q ELSE NULL END WHERE entry_id = :e"""),
        {"to": to, "q": quarantine_id, "e": e["entry_id"]})


async def _lock(c: AsyncConnection, eid: str, if_match: str | None) -> dict[str, Any]:
    e = one(await c.execute(text("SELECT * FROM memory.entries WHERE entry_id = :e FOR UPDATE"), {"e": eid}))
    if not e:
        raise HttpError(404, "entry_not_found", eid)
    if if_match is None:
        raise HttpError(428, "if_match_required", "If-Match: <state_seq> is required")
    if str(if_match).strip('"W/') != str(e["state_seq"]):
        raise HttpError(412, "state_seq_mismatch", f"entry is at state_seq {e['state_seq']}", state_seq=e["state_seq"])
    return e


async def entry_action(db: Database, admin: AdminPrincipal, eid: str, verb: str, body: dict[str, Any],
                       if_match: str | None, idem: str) -> dict[str, Any]:
    if verb not in TRANSITIONS:
        raise HttpError(404, "unknown_action", verb)
    sources, to = TRANSITIONS[verb]
    async with db.tx(admin=True) as c:
        e = await _lock(c, eid, if_match)
        if e["state"] not in sources:
            raise HttpError(409, "invalid_transition", f"{e['state']} -> {to} is not allowed")
        if to == "active":
            blocking = (await c.execute(text("""SELECT count(*) FROM memory.conflicts WHERE state = 'open'
                AND blocks_activation AND (entry_a = :e OR entry_b = :e)"""), {"e": eid})).scalar()
            if blocking:
                raise HttpError(409, "conflict_open", "resolve the open conflict before activating this entry")
        act, replay = await _action(c, admin, e["tenant_id"], verb, {"entry_id": eid}, idem, body,
                                    "memory.entry_transition")
        if not replay:
            qid = None
            if verb == "quarantine":
                qid = ulid("mqtn")
                await _quarantine_row(c, qid, e["tenant_id"], {"entry_id": eid}, body.get("reason") or "quarantine", 1,
                                      admin, act)
                await c.execute(text("""INSERT INTO memory.quarantine_members (quarantine_id, entry_id, tenant_id, prior_state)
                    VALUES (:q, :e, :t, :p)"""), {"q": qid, "e": eid, "t": e["tenant_id"], "p": e["state"]})
            await _transition(c, e, to, admin, act, f"admin_{verb}", body.get("reason"), qid)
    return await get_entry(db, eid)


async def edit_entry(db: Database, admin: AdminPrincipal, eid: str, body: dict[str, Any], if_match: str | None,
                     idem: str) -> dict[str, Any]:
    async with db.tx(admin=True) as c:
        e = await _lock(c, eid, if_match)
        if e["state"] in ("purged", "rejected", "superseded", "expired"):
            raise HttpError(409, "invalid_transition", f"cannot edit an entry in state {e['state']}")
        cur = one(await c.execute(text("SELECT * FROM memory.entry_versions WHERE entry_id = :e AND version = :v"),
                                  {"e": eid, "v": e["current_version"]}))
        act, replay = await _action(c, admin, e["tenant_id"], "edit", {"entry_id": eid}, idem, body, "memory.entry_edited")
        if not replay:
            p = _admin_proposal(body, cur)
            repo, globs, level = _admin_scope(p, cur)
            stmt = core.redact(p["statement"], 4000).strip()
            findings = core.safety_findings(stmt)
            if findings and not body.get("override_safety"):
                raise HttpError(422, "blocked_safety", f"safety rules fired: {findings}", rules=findings)
            ver = e["current_version"] + 1
            subject = p.get("subject_key") if p.get("subject_key") and core.SUBJECT_RE.match(p["subject_key"]) else None
            if e["memory_type"] == "preference" and level == "tenant":
                raise HttpError(422, "invalid_scope", "preferences need repo or path scope")
            await _insert_version(c, eid, ver, e["tenant_id"], e["memory_type"], level, repo, globs, subject, p, stmt,
                                  core.content_digest(e["memory_type"], level, repo, globs, stmt), "edit",
                                  admin.principal_id, body.get("change_note"))
            await c.execute(text("""UPDATE memory.entries SET current_version = :v, scope_level = :lvl, repo = :repo,
                subject_key = :sk, state_seq = state_seq + 1 WHERE entry_id = :e"""),
                {"v": ver, "lvl": level, "repo": repo, "sk": subject, "e": eid})
            if body.get("approve") and e["state"] == "candidate":
                e2 = one(await c.execute(text("SELECT * FROM memory.entries WHERE entry_id = :e"), {"e": eid}))
                await _transition(c, e2, "active", admin, act, "admin_edit_approve", body.get("reason"))
    return await get_entry(db, eid)


def _admin_proposal(body: dict[str, Any], cur: dict[str, Any] | None) -> dict[str, Any]:
    cur = cur or {}
    p = {"statement": str(body.get("statement") or cur.get("statement") or ""),
         "title": body.get("title", cur.get("title")), "rationale": body.get("rationale", cur.get("rationale")),
         "subject_key": body.get("subject_key", cur.get("subject_key")),
         "proposed_scope": {"repo": body.get("repo", cur.get("repo")),
                            "path_globs": body.get("path_globs", list(cur.get("path_globs") or [])),
                            "level": body.get("scope_level", cur.get("scope_level")),
                            "roles": body.get("roles", list(cur.get("roles") or []))}}
    if len(p["statement"].strip()) < 10:
        raise HttpError(422, "invalid_statement", "statement must have at least 10 characters")
    return p


def _admin_scope(p: dict[str, Any], cur: dict[str, Any] | None) -> tuple[str | None, list[str], str]:
    sc = p["proposed_scope"]
    level = sc.get("level") or "repo"
    repo = core.norm_repo(sc.get("repo")) if level in ("repo", "path") else None
    globs = [g for g in sc.get("path_globs") or [] if isinstance(g, str) and g][:20] if level == "path" else []
    if level not in ("tenant", "repo", "path"):
        raise HttpError(422, "invalid_scope", "scope_level must be tenant, repo or path (v0)")
    if level in ("repo", "path") and not repo:
        raise HttpError(422, "invalid_scope", "repo scope needs repo host/owner/name")
    if level == "path" and not globs:
        raise HttpError(422, "invalid_scope", "path scope needs path_globs")
    return repo, globs, level


async def create_entry(db: Database, admin: AdminPrincipal, body: dict[str, Any], idem: str) -> dict[str, Any]:
    tenant = body.get("tenant_id")
    if not tenant or not re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", tenant):
        raise HttpError(422, "invalid_tenant", "tenant_id required")
    stype = body.get("memory_type") or "rule"
    if stype not in ("rule", "negative", "procedure", "preference"):
        raise HttpError(422, "invalid_type", "memory_type must be rule, negative, procedure or preference")
    p = _admin_proposal(body, None)
    repo, globs, level = _admin_scope(p, None)
    if stype == "preference" and level == "tenant":
        raise HttpError(422, "invalid_scope", "preferences need repo or path scope")
    stmt = core.redact(p["statement"], 4000).strip()
    findings = core.safety_findings(stmt)
    if findings and not body.get("override_safety"):
        raise HttpError(422, "blocked_safety", f"safety rules fired: {findings}", rules=findings)
    subject = p.get("subject_key") if p.get("subject_key") and core.SUBJECT_RE.match(str(p["subject_key"])) else None
    state = "candidate" if body.get("as_candidate") else "active"
    async with db.tx(admin=True) as c:
        act, replay = await _action(c, admin, tenant, "create", {"tenant_id": tenant}, idem, body)
        if replay:
            eid = (await c.execute(text("SELECT target->>'entry_id' FROM memory.curator_actions WHERE action_id = :a"),
                                   {"a": act})).scalar()
            prev = (await c.execute(text("""SELECT entry_id FROM memory.provenance WHERE source_kind = 'curator'
                AND independence_key = :k LIMIT 1"""), {"k": core.sha256_digest(act)})).scalar()
            return await get_entry(db, eid or prev)
        eid = ulid("mem")
        await c.execute(text("""INSERT INTO memory.entries (entry_id, tenant_id, memory_type, origin, state, scope_level,
            repo, subject_key, created_by, enforced, confidence_alpha) VALUES (:e, :t, :ty, 'curator', :st, :lvl, :repo,
            :sk, :by, false, 4)"""), {"e": eid, "t": tenant, "ty": stype, "st": state, "lvl": level, "repo": repo,
                                      "sk": subject, "by": admin.principal_id})
        await _insert_version(c, eid, 1, tenant, stype, level, repo, globs, subject, p, stmt,
                              core.content_digest(stype, level, repo, globs, stmt), "create", admin.principal_id)
        await c.execute(text(_PROV_INSERT), {**_prov({"kind": "curator", "text": stmt}, "curator", act, {}, "", p),
                                             "author": None, "e": eid, "t": tenant, "v": 1})
        await c.execute(text("""INSERT INTO memory.state_transitions (entry_id, tenant_id, from_state, to_state,
            entry_version, actor_kind, actor_principal_id, curator_action_id, reason_code)
            VALUES (:e, :t, NULL, :st, 1, 'admin', :a, :act, 'admin_create')"""),
            {"e": eid, "t": tenant, "st": state, "a": admin.principal_id, "act": act})
    return await get_entry(db, eid)


async def _quarantine_row(c: AsyncConnection, qid: str, tenant: str, selector: dict[str, Any], reason: str, n: int,
                          admin: AdminPrincipal, act: str) -> None:
    await c.execute(text("""INSERT INTO memory.quarantines (quarantine_id, tenant_id, kind, selector, selector_digest,
        reason, entries_affected, created_by, curator_action_id) VALUES (:q, :t, 'quarantine', CAST(:s AS jsonb), :d,
        :r, :n, :by, :act)"""), {"q": qid, "t": tenant, "s": jdump(selector), "d": core.sha256_digest(jdump(selector)),
                                  "r": reason[:2000] or "quarantine", "n": n, "by": admin.principal_id, "act": act})


async def _match(c: AsyncConnection, sel: dict[str, Any]) -> list[dict[str, Any]]:
    where, params = ["e.state IN ('candidate', 'active', 'disabled', 'deprecated')"], {}
    if sel.get("tenant_id"):
        where.append("e.tenant_id = :t")
        params["t"] = sel["tenant_id"]
    sub = []
    for k, col in (("author_principal_id", "p.author_principal_id"), ("source_kind", "p.source_kind"),
                   ("job_id", "p.job_id")):
        if sel.get(k):
            sub.append(f"{col} = :{k}")
            params[k] = sel[k]
    if sel.get("repo"):
        where.append("e.repo = :repo")
        params["repo"] = core.norm_repo(sel["repo"]) or sel["repo"]
    if sel.get("entry_ids"):
        where.append("e.entry_id = ANY(CAST(:ids AS text[]))")
        params["ids"] = list(sel["entry_ids"])
    if sub:
        where.append("EXISTS (SELECT 1 FROM memory.provenance p WHERE p.entry_id = e.entry_id AND " + " AND ".join(sub) + ")")
    if len(where) == 1 or (len(where) == 2 and "t" in params and not sub and "repo" not in params and "ids" not in params):
        raise HttpError(422, "selector_too_broad", "selector needs author_principal_id, source_kind, job_id, repo or entry_ids")
    return rows(await c.execute(text("SELECT e.* FROM memory.entries e WHERE " + " AND ".join(where)
                                     + " ORDER BY e.entry_id LIMIT 5000"), params))


async def _blast(c: AsyncConnection, ids: list[str]) -> dict[str, Any]:
    pat = [i + "@%" for i in ids]
    jobs = rows(await c.execute(text("""SELECT DISTINCT job_id FROM memory.injections
        WHERE EXISTS (SELECT 1 FROM unnest(injected_refs) r WHERE r LIKE ANY(CAST(:p AS text[])))"""), {"p": pat}))
    snaps = rows(await c.execute(text("""SELECT DISTINCT snapshot_id FROM memory.snapshot_items
        WHERE entry_id = ANY(CAST(:ids AS text[]))"""), {"ids": ids}))
    return {"jobs": [j["job_id"] for j in jobs], "snapshots": [s["snapshot_id"] for s in snaps]}


async def bulk_quarantine(db: Database, admin: AdminPrincipal, body: dict[str, Any], idem: str) -> dict[str, Any]:
    sel = {k: v for k, v in (body.get("selector") or {}).items() if v}
    async with db.tx(admin=True) as c:
        ents = await _match(c, sel)
        blast = await _blast(c, [e["entry_id"] for e in ents]) if ents else {"jobs": [], "snapshots": []}
        out = {"dry_run": bool(body.get("dry_run")), "entries": [e["entry_id"] for e in ents], "count": len(ents), **blast}
        if body.get("dry_run") or not ents:
            return out
        tenants = sorted({e["tenant_id"] for e in ents})
        out["quarantine_ids"] = []
        for t in tenants:
            mine = [e for e in ents if e["tenant_id"] == t]
            act, replay = await _action(c, admin, t, "quarantine", {"selector": sel}, f"{idem}:{t}", body,
                                        "memory.quarantine_created")
            if replay:
                continue
            qid = ulid("mqtn")
            await _quarantine_row(c, qid, t, sel, str(body.get("reason") or "bulk quarantine"), len(mine), admin, act)
            for e in mine:
                await c.execute(text("""INSERT INTO memory.quarantine_members (quarantine_id, entry_id, tenant_id,
                    prior_state) VALUES (:q, :e, :t, :p)"""), {"q": qid, "e": e["entry_id"], "t": t, "p": e["state"]})
                await _transition(c, e, "quarantined", admin, act, "admin_quarantine", body.get("reason"), qid)
            out["quarantine_ids"].append(qid)
    return out


async def purge(db: Database, s3: Any, admin: AdminPrincipal, body: dict[str, Any], idem: str) -> dict[str, Any]:
    ids = [i for i in body.get("entry_ids") or [] if isinstance(i, str)]
    if not ids:
        raise HttpError(422, "entry_ids_required", "entry_ids required")
    reason = body.get("reason_code") if body.get("reason_code") in ("poisoned", "secret_leak", "pii", "legal_request",
                                                                     "tenant_offboarding", "curator") else "curator"
    async with db.tx(admin=True) as c:
        ents = rows(await c.execute(text("SELECT * FROM memory.entries WHERE entry_id = ANY(CAST(:ids AS text[]))"),
                                    {"ids": ids}))
        if len(ents) != len(set(ids)):
            raise HttpError(404, "entry_not_found", "unknown entry in entry_ids")
        blast = await _blast(c, ids)
        out: dict[str, Any] = {"dry_run": bool(body.get("dry_run")), "entries": ids, **blast}
        if body.get("dry_run"):
            return out
        purges = []
        for t in sorted({e["tenant_id"] for e in ents}):
            mine = [e["entry_id"] for e in ents if e["tenant_id"] == t]
            act, replay = await _action(c, admin, t, "purge", {"entry_ids": mine}, f"{idem}:{t}", body,
                                        "memory.purge_requested")
            if replay:
                continue
            pid, dl = ulid("mprg"), ulid("del")
            await s3.put("audit", f"tenants/{t}/deletion-ledger/{dl}.json", json.dumps(  # before any delete (R1-102)
                {"schema": "replicant.deletion-ledger/v1", "deletion_ledger_id": dl, "purge_id": pid, "tenant_id": t,
                 "entry_ids": mine, "requested_by": admin.principal_id, "at": _now().isoformat()}).encode())
            await c.execute(text("""INSERT INTO memory.purges (purge_id, tenant_id, path, requested_by, reason_code,
                reason_text, entry_ids, deletion_ledger_ids, status, curator_action_id) VALUES (:p, :t, 'admin', :by, :rc,
                :rt, CAST(:ids AS text[]), CAST(:dl AS text[]), 'db_purged', :act)"""),
                {"p": pid, "t": t, "by": admin.principal_id, "rc": reason, "rt": str(body.get("reason_text") or reason)[:2000],
                 "ids": mine, "dl": [dl], "act": act})
            for eid in mine:
                await c.execute(text("SELECT memory.purge_entry(:e, :p)"), {"e": eid, "p": pid})
            purges.append((pid, t, mine))
        # propagation: snapshot exports re-materialized without the purged content; injection artifacts deleted
        for pid, t, mine in purges:
            snaps = rows(await c.execute(text("""SELECT DISTINCT s.snapshot_id, s.export_key FROM memory.snapshots s
                JOIN memory.snapshot_items i ON i.snapshot_id = s.snapshot_id WHERE i.entry_id = ANY(CAST(:ids AS text[]))"""),
                {"ids": mine}))
            for sn in snaps:
                items = rows(await c.execute(text("""SELECT i.entry_id || '@' || i.version AS ref, i.section, i.precedence_rank,
                    v.statement FROM memory.snapshot_items i JOIN memory.entry_versions v ON v.entry_id = i.entry_id
                    AND v.version = i.version WHERE i.snapshot_id = :s ORDER BY i.precedence_rank"""), {"s": sn["snapshot_id"]}))
                doc = {"schema": "replicant.memory-snapshot-export/v1", "snapshot_id": sn["snapshot_id"], "tenant_id": t,
                       "purge_id": pid, "items": [{"ref": x["ref"], "section": x["section"],
                                                   "precedence_rank": x["precedence_rank"],
                                                   **({"statement": x["statement"]} if x["statement"] is not None
                                                      else {"purged": True})} for x in items]}
                rkey = sn["export_key"].removesuffix(".json") + f".purged-{pid}.json"
                sha = await s3.put("memory", rkey, json.dumps(doc, sort_keys=True).encode())
                await c.execute(text("""INSERT INTO memory.snapshot_export_replacements (snapshot_id, purge_id, tenant_id,
                    replacement_key, replacement_sha256) VALUES (:s, :p, :t, :k, :sha) ON CONFLICT DO NOTHING"""),
                    {"s": sn["snapshot_id"], "p": pid, "t": t, "k": rkey, "sha": sha})
                await s3.delete("memory", sn["export_key"])
            arts = rows(await c.execute(text("""SELECT artifact_key FROM memory.injections WHERE EXISTS (SELECT 1 FROM
                unnest(injected_refs) r WHERE r LIKE ANY(CAST(:p AS text[])))"""), {"p": [i + "@%" for i in mine]}))
            for a in arts:
                await s3.delete("artifacts", a["artifact_key"])
            await c.execute(text("""UPDATE memory.purges SET status = 'complete', completed_at = now(),
                propagation = CAST(:pr AS jsonb) WHERE purge_id = :p"""),
                {"p": pid, "pr": jdump({"snapshot_exports": len(snaps), "memory_injection": len(arts)})})
            await _emit(c, {"event_type": "memory.purge_completed", "tenant_id": t, "actor_principal_id": admin.principal_id,
                            "details": {"purge_id": pid, "entries": len(mine)}})
        out["purge_ids"] = [p[0] for p in purges]
    return out


async def export(db: Database, s3: Any, admin: AdminPrincipal, body: dict[str, Any], idem: str) -> dict[str, Any]:
    tenant = body.get("tenant_id")
    if not tenant:
        raise HttpError(422, "invalid_tenant", "tenant_id required")
    flt = {k: body.get(k) for k in ("repo", "state", "memory_type") if body.get(k)}
    states = str(flt.get("state") or "candidate,active,disabled,deprecated,quarantined").split(",")
    async with db.tx(admin=True) as c:
        res = rows(await c.execute(text(LIST_SQL + """ WHERE e.tenant_id = :t AND e.state = ANY(CAST(:st AS text[]))
            AND e.state <> 'purged' AND (CAST(:repo AS text) IS NULL OR e.repo = :repo) ORDER BY e.entry_id"""),
            {"t": tenant, "st": states, "repo": core.norm_repo(flt.get("repo")) if flt.get("repo") else None}))
        act, _ = await _action(c, admin, tenant, "export", {"filter": flt}, idem, body, "memory.export_created")
        xid = ulid("mexp")
        doc = {"schema": "replicant.memory-export/v1", "export_id": xid, "tenant_id": tenant, "filter": flt,
               "generated_at": _now().isoformat(), "entries": _jsonable(res)}
        key = f"tenants/{tenant}/exports/{xid}/entries.json"
        sha = await s3.put("memory", key, json.dumps(doc, sort_keys=True).encode())
        await c.execute(text("""INSERT INTO memory.exports (export_id, tenant_id, requested_by, filter, status, object_key,
            sha256, entry_count, completed_at, curator_action_id) VALUES (:x, :t, :by, CAST(:f AS jsonb), 'complete', :k,
            :sha, :n, now(), :act)"""), {"x": xid, "t": tenant, "by": admin.principal_id, "f": jdump(flt), "k": key,
                                          "sha": sha, "n": len(res), "act": act})
    return {**doc, "object_key": key, "sha256": sha}


async def list_conflicts(db: Database, q: dict[str, Any]) -> list[dict[str, Any]]:
    async with db.tx(admin=True) as c:
        res = rows(await c.execute(text("""SELECT k.*, va.statement AS statement_a, vb.statement AS statement_b,
            ea.state AS state_a, eb.state AS state_b, ea.state_seq AS seq_a, eb.state_seq AS seq_b
            FROM memory.conflicts k
            JOIN memory.entry_versions va ON va.entry_id = k.entry_a AND va.version = k.version_a
            JOIN memory.entry_versions vb ON vb.entry_id = k.entry_b AND vb.version = k.version_b
            JOIN memory.entries ea ON ea.entry_id = k.entry_a JOIN memory.entries eb ON eb.entry_id = k.entry_b
            WHERE (CAST(:st AS text) IS NULL OR k.state = :st) ORDER BY k.detected_at DESC LIMIT 200"""),
            {"st": q.get("state") or "open"}))
    return _jsonable(res)


async def resolve_conflict(db: Database, admin: AdminPrincipal, cid: str, body: dict[str, Any], idem: str) -> dict[str, Any]:
    resolution = body.get("resolution")
    if resolution not in ("keep_a", "keep_b", "both_valid", "neither"):
        raise HttpError(422, "invalid_resolution", "resolution: keep_a | keep_b | both_valid | neither")
    async with db.tx(admin=True) as c:
        k = one(await c.execute(text("SELECT * FROM memory.conflicts WHERE conflict_id = :c FOR UPDATE"), {"c": cid}))
        if not k:
            raise HttpError(404, "conflict_not_found", cid)
        if k["state"] != "open":
            raise HttpError(409, "conflict_closed", k["state"])
        act, replay = await _action(c, admin, k["tenant_id"], "resolve_conflict", {"conflict_id": cid}, idem, body,
                                    "memory.conflict_resolved")
        if replay:
            return {"conflict_id": cid, "replayed": True}
        losers = {"keep_a": [k["entry_b"]], "keep_b": [k["entry_a"]], "both_valid": [],
                  "neither": [k["entry_a"], k["entry_b"]]}[resolution]
        for eid in losers:
            e = one(await c.execute(text("SELECT * FROM memory.entries WHERE entry_id = :e FOR UPDATE"), {"e": eid}))
            to = "rejected" if e["state"] == "candidate" else ("deprecated" if e["state"] in ("active", "disabled") else None)
            if to:
                await _transition(c, e, to, admin, act, "conflict_resolved", resolution)
        await c.execute(text("""UPDATE memory.conflicts SET state = 'resolved', resolution = :r, resolved_at = now(),
            resolved_by = :a, curator_action_id = :act WHERE conflict_id = :c"""),
            {"r": resolution, "a": admin.principal_id, "act": act, "c": cid})
    return {"conflict_id": cid, "resolution": resolution, "changed": losers}


async def analytics(db: Database, report: str, q: dict[str, Any]) -> dict[str, Any]:
    async with db.tx(admin=True) as c:
        if report == "entries":
            # per active entry: outcomes of jobs on the same repo with and without the entry injected
            res = rows(await c.execute(text("""
              WITH exp AS (SELECT DISTINCT split_part(r, '@', 1) AS entry_id, i.job_id
                             FROM memory.injections i, unnest(i.injected_refs) r)
              SELECT e.entry_id, e.repo, e.memory_type, e.injection_count, v.statement,
                     count(o.job_id) FILTER (WHERE x.job_id IS NOT NULL) AS jobs_with,
                     count(o.job_id) FILTER (WHERE x.job_id IS NULL) AS jobs_without,
                     avg(o.changes_requested_reviews) FILTER (WHERE x.job_id IS NOT NULL)::float AS changes_requested_with,
                     avg(o.changes_requested_reviews) FILTER (WHERE x.job_id IS NULL)::float AS changes_requested_without,
                     avg(o.review_rounds) FILTER (WHERE x.job_id IS NOT NULL)::float AS review_rounds_with,
                     avg(o.review_rounds) FILTER (WHERE x.job_id IS NULL)::float AS review_rounds_without,
                     avg(o.first_pass_qa::int) FILTER (WHERE x.job_id IS NOT NULL)::float AS first_pass_qa_with,
                     avg(o.first_pass_qa::int) FILTER (WHERE x.job_id IS NULL)::float AS first_pass_qa_without
                FROM memory.entries e JOIN memory.entry_versions v ON v.entry_id = e.entry_id AND v.version = e.current_version
                LEFT JOIN memory.job_outcomes o ON o.tenant_id = e.tenant_id AND o.repo = e.repo
                LEFT JOIN exp x ON x.entry_id = e.entry_id AND x.job_id = o.job_id
               WHERE e.state = 'active' GROUP BY e.entry_id, e.repo, e.memory_type, e.injection_count, v.statement
               ORDER BY e.injection_count DESC LIMIT 200""")))
            return {"report": report, "rows": _jsonable(res)}
        if report == "feedback":
            res = rows(await c.execute(text("""
              SELECT date_trunc('week', decided_at) AS week, split_part(source_key, ':', 1) AS source, count(*) AS items,
                     count(*) FILTER (WHERE decision = 'evidence_merged') AS repeated,
                     count(*) FILTER (WHERE decision IN ('candidate_created', 'conflict_opened')) AS new_candidates,
                     count(*) FILTER (WHERE decision LIKE 'blocked_%') AS blocked
                FROM memory.feedback_items GROUP BY 1, 2 ORDER BY 1 DESC, 2""")))
            for r in res:
                r["repeated_rate"] = round(r["repeated"] / r["items"], 4) if r["items"] else None
            return {"report": report, "rows": _jsonable(res)}
        if report == "summary":
            counts = rows(await c.execute(text("SELECT state, count(*) AS n FROM memory.entries GROUP BY state")))
            inj = rows(await c.execute(text("""SELECT count(*) AS runs, count(DISTINCT job_id) AS jobs,
                avg(cardinality(injected_refs))::float AS avg_entries FROM memory.injections""")))
            outc = rows(await c.execute(text("""SELECT count(*) AS jobs, avg(review_rounds)::float AS avg_review_rounds,
                avg(changes_requested_reviews)::float AS avg_changes_requested, avg(first_pass_qa::int)::float AS first_pass_qa_rate
                FROM memory.job_outcomes""")))
            return {"report": report, "states": {r["state"]: r["n"] for r in counts}, "injections": _jsonable(inj[0]),
                    "outcomes": _jsonable(outc[0])}
    raise HttpError(404, "unknown_report", report)
