"""The write path: feedback items -> candidate entries (safety, dedupe, conflicts), recurrence findings and job
outcomes."""
from __future__ import annotations

import re
from typing import Any

from replicant_ids import ulid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from memory_service import core
from memory_service.db import Database, jdump, one
from memory_service.store import EXTRACTOR_DEFAULT, HUMAN_KINDS, ORIGIN_BY_KIND, SOURCE_PREFIX, SVC, _key, _now, _ok


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
