"""Curator (admin) verbs: entry reads, state transitions, edits, manual entries, bulk quarantine, purge, export and
conflict resolution."""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from memory_service import core
from memory_service.auth import AdminPrincipal, HttpError
from memory_service.candidates import _PROV_INSERT, _insert_version, _prov, recount
from memory_service.db import Database, jdump, one, rows
from memory_service.store import ITEM_SQL, REVIEW_DUE_DAYS, STALE_DAYS, TRANSITIONS, _emit, _item, _jsonable, _now
from replicant_ids import ulid

# ================================================================================================ admin: reads
LIST_SQL = """
SELECT e.entry_id, e.tenant_id, e.memory_type, e.origin, e.state, e.state_seq, e.scope_level, e.repo, e.subject_key,
       e.current_version, e.confidence::float AS confidence, e.evidence_count, e.injection_count, e.created_at,
       e.state_changed_at, e.last_injected_at, v.title, v.statement, v.rationale, v.path_globs,
       (SELECT p.source_kind FROM memory.provenance p WHERE p.entry_id = e.entry_id ORDER BY p.provenance_id LIMIT 1) AS source_kind,
       (SELECT count(*) FROM memory.conflicts k WHERE k.state = 'open' AND (k.entry_a = e.entry_id OR k.entry_b = e.entry_id)) AS open_conflicts,
       e.independent_evidence_count, e.distinct_author_count, e.activation_hold, e.hold_reason, e.pr_outcome,
       e.review_due_at, e.last_confirmed_at,
       (SELECT count(*) FROM memory.provenance p WHERE p.entry_id = e.entry_id AND p.withdrawn_at IS NULL
           AND p.source_trust = 'human') AS human_sources,
       (SELECT count(*) FROM memory.provenance p WHERE p.entry_id = e.entry_id AND p.withdrawn_at IS NULL
           AND p.source_trust = 'machine') AS machine_sources,
       (SELECT count(*) FROM memory.entry_flags f WHERE f.entry_id = e.entry_id AND f.status = 'open') AS open_flags,
       (SELECT p.generalization->'documented_in' FROM memory.provenance p WHERE p.entry_id = e.entry_id
           AND p.generalization->'documented_in' IS NOT NULL ORDER BY p.provenance_id DESC LIMIT 1) AS documented_in,
       (SELECT p.generalization->'verification' FROM memory.provenance p WHERE p.entry_id = e.entry_id
           AND p.generalization->'verification' IS NOT NULL ORDER BY p.provenance_id DESC LIMIT 1) AS verification,
       (SELECT p.generalization->>'label' FROM memory.provenance p WHERE p.entry_id = e.entry_id
           AND p.generalization->>'label' IS NOT NULL ORDER BY p.provenance_id DESC LIMIT 1) AS label,
       (e.state = 'active' AND coalesce(e.last_injected_at, e.state_changed_at) < now() - interval '{stale} days') AS stale,
       (e.state = 'active' AND e.review_due_at <= now()) AS review_due
  FROM memory.entries e JOIN memory.entry_versions v ON v.entry_id = e.entry_id AND v.version = e.current_version
""".replace("{stale}", str(int(STALE_DAYS)))


def _trust(r: dict[str, Any]) -> dict[str, Any]:
    """MEM-D13 label: machine when no live human-sourced provenance row corroborates the entry."""
    r["source_trust"] = "machine" if not r.get("human_sources") and r.get("machine_sources") else "human"
    return r


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
    if q.get("review") in ("due", "flagged", "due_or_flagged"):  # MEM-D14 "Due for review / flagged"
        where.append("((e.state = 'active' AND e.review_due_at <= now()) OR EXISTS (SELECT 1 FROM memory.entry_flags f "
                     "WHERE f.entry_id = e.entry_id AND f.status = 'open'))")
    if q.get("job_id"):
        where.append("EXISTS (SELECT 1 FROM memory.provenance p WHERE p.entry_id = e.entry_id AND p.job_id = :job)")
        params["job"] = q["job_id"]
    params["lim"] = min(int(q.get("limit") or 200), 500)
    async with db.tx(admin=True) as c:
        res = rows(await c.execute(text(LIST_SQL + " WHERE " + " AND ".join(where)
                                        + " ORDER BY e.created_at DESC LIMIT :lim"), params))
    return [_jsonable(_trust(r)) for r in res]


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
            withdrawn_reason, source_trust, independence_key, cycle,
            row_number() OVER (PARTITION BY independence_key ORDER BY provenance_id) > 1 AS repeat
            FROM memory.provenance WHERE entry_id = :e ORDER BY provenance_id"""), {"e": eid}))
        trans = rows(await c.execute(text("""SELECT from_state, to_state, entry_version, at, actor_kind, actor_principal_id,
            reason_code, reason_text FROM memory.state_transitions WHERE entry_id = :e ORDER BY transition_id"""), {"e": eid}))
        jobs = rows(await c.execute(text("""SELECT job_id, run_id, role, node_key, created_at, snapshot_id
            FROM memory.injections WHERE EXISTS (SELECT 1 FROM unnest(injected_refs) r WHERE r LIKE :p)
            ORDER BY created_at DESC LIMIT 100"""), {"p": eid + "@%"}))
        conflicts = rows(await c.execute(text("""SELECT conflict_id, entry_a, entry_b, kind, state, resolution, detected_at
            FROM memory.conflicts WHERE entry_a = :e OR entry_b = :e ORDER BY detected_at DESC"""), {"e": eid}))
        similar = rows(await c.execute(text("""SELECT e2.entry_id, e2.state, e2.memory_type, v2.statement,
            similarity(v2.statement, :st)::float AS similarity FROM memory.entries e2 JOIN memory.entry_versions v2
            ON v2.entry_id = e2.entry_id AND v2.version = e2.current_version WHERE e2.tenant_id = :t AND e2.entry_id <> :e
            AND e2.state IN ('candidate', 'active') AND similarity(v2.statement, :st) >= 0.3
            ORDER BY similarity(v2.statement, :st) DESC LIMIT 5"""), {"st": e["statement"], "t": e["tenant_id"], "e": eid}))
        eff = one(await c.execute(text(EFFECT_SQL), {"e": eid, "p": eid + "@%", "st": e["statement"]})) or {}
        flags = rows(await c.execute(text("""SELECT flag_id, kind, source, reason_code, reason_text, status, created_at,
            due_at, job_id FROM memory.entry_flags WHERE entry_id = :e ORDER BY flag_id DESC LIMIT 50"""), {"e": eid}))
        cur = one(await c.execute(text(ITEM_SQL.format(extra="") + " WHERE e.entry_id = :e"), {"e": eid}))
    preview = core.render("msnap_preview", [_item(cur)]) if cur else ""
    jobs_inj, rec = int(eff.get("jobs_injected") or 0), int(eff.get("jobs_recurred") or 0)
    effectiveness = {"jobs_injected": jobs_inj, "jobs_recurred": rec,
                     "signal": "no_data" if not jobs_inj else ("recurred" if rec else "no_recurrence")}
    return _jsonable({**_trust(e), "versions": versions, "provenance": prov, "transitions": trans, "jobs": jobs,
                      "conflicts": conflicts, "similar": similar, "effectiveness": effectiveness, "flags": flags,
                      "preview": preview})


# MEM-D14 effectiveness: of the jobs that had the entry injected, how many still received feedback similar to it
# (trigram >= 0.6 against the statement or one of its source excerpts, or merged as evidence into it).
EFFECT_SQL = """
WITH inj AS (SELECT job_id FROM memory.injections WHERE EXISTS (SELECT 1 FROM unnest(injected_refs) r WHERE r LIKE :p)
              GROUP BY job_id)
SELECT count(*) AS jobs_injected,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM memory.feedback_items f WHERE f.job_id = inj.job_id
           AND f.source_key !~ '^(qa|review|outcome|scan|agent):'
           AND ((f.decision = 'evidence_merged' AND f.entry_id = :e)
                OR similarity(coalesce(f.detail->>'text', ''), :st) >= 0.6
                OR EXISTS (SELECT 1 FROM memory.provenance p WHERE p.entry_id = :e AND p.source_excerpt IS NOT NULL
                             AND similarity(coalesce(f.detail->>'text', ''), p.source_excerpt) >= 0.6)))) AS jobs_recurred
  FROM inj"""



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
        quarantine_id = CASE WHEN :to = 'quarantined' THEN :q ELSE NULL END,
        review_due_at = CASE WHEN :to = 'active' THEN now() + make_interval(days => :days) ELSE review_due_at END
        WHERE entry_id = :e"""), {"to": to, "q": quarantine_id, "e": e["entry_id"], "days": REVIEW_DUE_DAYS})


async def _approvable(c: AsyncConnection, e: dict[str, Any]) -> None:
    """Approval gates: MEM-D12 (held until the PR outcome is known) and MEM-D13 (machine-sourced entries need at least
    one human-sourced provenance row). Rejecting stays possible."""
    if e.get("activation_hold") and e.get("hold_reason") == "awaiting_merge":
        raise HttpError(409, "awaiting_merge", "held until the job's PR merges with the fix (it can still be rejected)")
    r = one(await c.execute(text("""SELECT count(*) FILTER (WHERE source_trust = 'human') AS human,
        count(*) FILTER (WHERE source_trust = 'machine') AS machine FROM memory.provenance
        WHERE entry_id = :e AND withdrawn_at IS NULL"""), {"e": e["entry_id"]})) or {}
    if r.get("machine") and not r.get("human"):
        raise HttpError(409, "needs_human_corroboration",
                        "machine-sourced: approve only after a human-sourced source corroborates it")


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
    if verb == "mark_reviewed":
        return await mark_reviewed(db, admin, eid, body, if_match, idem)
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
            if verb == "approve":
                await _approvable(c, e)
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


async def mark_reviewed(db: Database, admin: AdminPrincipal, eid: str, body: dict[str, Any], if_match: str | None,
                        idem: str) -> dict[str, Any]:
    """MEM-D14: an admin re-checked an active entry: push review_due_at out and close its open lifecycle flags."""
    async with db.tx(admin=True) as c:
        e = await _lock(c, eid, if_match)
        if e["state"] != "active":
            raise HttpError(409, "invalid_transition", "only active entries are reviewed")
        act, replay = await _action(c, admin, e["tenant_id"], "mark_reviewed", {"entry_id": eid}, idem, body)
        if not replay:
            await c.execute(text("""UPDATE memory.entries SET review_due_at = now() + make_interval(days => :d)
                WHERE entry_id = :e"""), {"d": REVIEW_DUE_DAYS, "e": eid})
            await c.execute(text("""UPDATE memory.entry_flags SET status = 'actioned', handled_by = :a, handled_at = now(),
                curator_action_id = :act WHERE entry_id = :e AND status = 'open' AND source = 'lifecycle'"""),
                {"a": admin.principal_id, "act": act, "e": eid})
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
                await _approvable(c, e)
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
            repo, subject_key, created_by, enforced, confidence_alpha, review_due_at) VALUES (:e, :t, :ty, 'curator', :st,
            :lvl, :repo, :sk, :by, false, 4, CASE WHEN :st = 'active' THEN now() + make_interval(days => :d) END)"""),
            {"e": eid, "t": tenant, "ty": stype, "st": state, "lvl": level, "repo": repo, "sk": subject,
             "by": admin.principal_id, "d": REVIEW_DUE_DAYS})
        await _insert_version(c, eid, 1, tenant, stype, level, repo, globs, subject, p, stmt,
                              core.content_digest(stype, level, repo, globs, stmt), "create", admin.principal_id)
        await c.execute(text(_PROV_INSERT), {**_prov({"kind": "curator", "text": stmt}, "curator", act, {}, "", p),
                                             "author": None, "e": eid, "t": tenant, "v": 1})
        await recount(c, eid)
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
