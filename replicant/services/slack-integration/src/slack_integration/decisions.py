"""Gate decisions from Slack (approvals.md §4.4, identity.md E4/E7; NOTIFY-D12).

block_actions `gate_approve` -> decide now (the button carries a confirm dialog); `gate_reject` -> modal for the
required comment -> view_submission -> decide. Every click: signature already verified by the route; dedupe on
(team, user, action_ts) in slack_app.interaction_dedupe; the clicker is resolved through identity.links (unlinked ->
ephemeral link prompt); a slack_app.pending_decisions row (decision_id = derived("dec", "slack", user:action_ts)) is
written before control-plane-api `POST /internal/v1/gates/{gate_id}/decisions` (Idempotency-Key = decision_id), which
applies the same authorization as the UI (gate:decide, separation of duties, digest, channel, gate state) and sends
the Update `gate.decide`. The original message is re-rendered in place with the outcome; refusals that leave the gate
open are ephemeral.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import httpx

from replicant_ids import derived
from slack_integration.blocks import reject_modal, resolved_blocks
from slack_integration.config import Settings, read_secret
from slack_integration.repo import Pending, Repo
from slack_integration.slack import SlackFailure

log = logging.getLogger("slack_integration.decisions")
VERB = {"approve": "Approved", "reject": "Rejected"}
REFUSAL = {
    "not_in_approver_group": "You can't decide this gate: you are not an eligible approver for it.",
    "self_approval_forbidden": "You can't decide this gate: you requested this job.",
    "channel_not_allowed": "This gate can't be decided from Slack. Open it in Replicant.",
    "identity_unlinked": "Link your Slack account to Replicant to decide on gates.",
    "actor_not_human": "Only people can decide gates.",
    "decision_not_allowed": "That decision isn't available for this gate.",
}


def _log(event: str, **kw: Any) -> None:
    log.info(json.dumps({"msg": event, **kw}))


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Decisions:
    def __init__(self, s: Settings, repo: Repo, slack: Any, team_id: Any,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = s
        self.repo = repo
        self.slack = slack
        self.team_id = team_id          # callable -> current team id
        self._transport = transport

    # ------------------------------------------------------------------ control-plane-api
    async def _post_decision(self, p: Pending, login: str | None) -> tuple[int, dict[str, Any]]:
        body: dict[str, Any] = {
            "schema": "replicant.msg.gate-decide/v1", "decision_id": p.decision_id, "gate_id": p.gate_id,
            "decision": p.decision, "channel": "slack", "presented_digest": p.presented_digest,
            "decided_at": now_iso(),
            "actor": {"principal_id": p.principal_id,
                      "channel_identity": {"kind": "slack", "id": p.slack_user_id, **({"login": login} if login else {})}},
            "channel_ref": {"slack_channel_id": p.channel_id[:32], "slack_message_ts": p.message_ts[:32]}}
        if p.comment:
            body["comment"] = p.comment[:8000]
        tok = read_secret(self.s.control_plane_token_file)
        headers = {"Idempotency-Key": f'"{p.decision_id}"', **({"Authorization": f"Bearer {tok}"} if tok else {})}
        try:
            async with httpx.AsyncClient(timeout=20, transport=self._transport) as c:
                r = await c.post(f"{self.s.control_plane_url}/internal/v1/gates/{p.gate_id}/decisions", json=body,
                                 headers=headers)
        except httpx.HTTPError as e:
            return 0, {"code": type(e).__name__}
        try:
            data = r.json()
        except ValueError:
            data = {}
        return r.status_code, data if isinstance(data, dict) else {}

    async def waiting_for(self, principal_id: str) -> dict[str, Any] | None:
        tok = read_secret(self.s.control_plane_token_file)
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as c:
                r = await c.get(f"{self.s.control_plane_url}/internal/v1/principals/{principal_id}/waiting",
                                headers={"Authorization": f"Bearer {tok}"} if tok else {})
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    # ------------------------------------------------------------------ interactions
    async def handle(self, payload: dict[str, Any]) -> None:
        t = payload.get("type")
        if t == "block_actions":
            for a in payload.get("actions") or []:
                if a.get("action_id") in ("gate_approve", "gate_reject"):
                    await self._block_action(payload, a)
        elif t == "view_submission" and (payload.get("view") or {}).get("callback_id") == "gate_reject_modal":
            await self._reject_submitted(payload)

    async def _resolve(self, user_id: str, channel: str) -> tuple[str, str | None] | None:
        who = await self.repo.principal_for_slack(user_id, self.team_id())
        if who is None:
            await self._ephemeral(channel, user_id, REFUSAL["identity_unlinked"])
        return who

    async def _block_action(self, payload: dict[str, Any], a: dict[str, Any]) -> None:
        user = (payload.get("user") or {}).get("id", "")
        team = (payload.get("team") or {}).get("id", "")
        cont = payload.get("container") or {}
        channel = cont.get("channel_id") or (payload.get("channel") or {}).get("id", "")
        ts = cont.get("message_ts", "")
        action_ts = str(a.get("action_ts") or payload.get("trigger_id") or "")
        if not await self.repo.first_interaction(team, user, action_ts):
            _log("interaction_duplicate", user=user, action_ts=action_ts)
            return
        try:
            v = json.loads(a.get("value") or "{}")
        except ValueError:
            return
        who = await self._resolve(user, channel)
        if who is None:
            return
        if a["action_id"] == "gate_reject":
            try:   # trigger_id expires in 3 s: open the modal before anything else
                await self.slack.open_view(trigger_id=payload.get("trigger_id", ""),
                                           view=reject_modal(a.get("value", ""), channel, ts))
            except SlackFailure as e:
                _log("modal_failed", code=e.code)
            return
        await self.decide(Pending(derived("dec", "slack", f"{user}:{action_ts}"), v.get("g", ""), who[0], user,
                                  "approve", v.get("d", ""), None, channel, ts), who[1])

    async def _reject_submitted(self, payload: dict[str, Any]) -> None:
        view = payload.get("view") or {}
        user = (payload.get("user") or {}).get("id", "")
        try:
            meta = json.loads(view.get("private_metadata") or "{}")
            v = json.loads(meta.get("v") or "{}")
        except ValueError:
            return
        comment = ((((view.get("state") or {}).get("values") or {}).get("comment") or {}).get("text") or {}).get("value")
        who = await self._resolve(user, meta.get("c", ""))
        if who is None or not comment:
            return
        key = view.get("id") or view.get("hash") or payload.get("trigger_id") or ""
        await self.decide(Pending(derived("dec", "slack", f"{user}:{key}"), v.get("g", ""), who[0], user, "reject",
                                  v.get("d", ""), comment, meta.get("c", ""), meta.get("t", "")), who[1])

    async def decide(self, p: Pending, login: str | None, *, retry: bool = False) -> str:
        if not retry:
            await self.repo.put_pending(p, None)
        code, data = await self._post_decision(p, login)
        _log("gate_decision_sent", decision_id=p.decision_id, gate_id=p.gate_id, decision=p.decision, http=code,
             outcome=data.get("outcome") or data.get("state") or data.get("code"))
        who = (login or "").capitalize() or "you"
        if code == 200 and data.get("outcome") in ("accepted", "recorded"):
            mark = "✓ " if p.decision == "approve" else ""
            line = (f"{mark}{VERB.get(p.decision, p.decision)} by {who} in Slack · "
                    f"{dt.datetime.now(dt.UTC).strftime('%H:%M UTC')}")
            if data.get("outcome") == "recorded":
                line = (f"{data.get('approvals_recorded')} of {data.get('approvals_required')} approvals · {who} "
                        "approved in Slack · waiting for another approver")
            else:
                await self._resolve_message(p.channel_id, p.message_ts, line, "approved" if p.decision == "approve"
                                            else "rejected")
            await self.repo.finish_pending(p.decision_id, data["outcome"] if data["outcome"] == "accepted" else "accepted",
                                           data)
            await self._ephemeral(p.channel_id, p.slack_user_id, "Approved. Replicant will publish the change and open "
                                  "the PR." if p.decision == "approve" else "Rejected. The job ends without publishing.")
            return "accepted"
        if code == 200 and data.get("outcome") == "superseded":
            wd = data.get("winning_decision") or {}
            await self._resolve_message(p.channel_id, p.message_ts, _already(wd), "superseded")
            await self._ephemeral(p.channel_id, p.slack_user_id, f"Not recorded: {_already(wd)}.")
            await self.repo.finish_pending(p.decision_id, "superseded", data)
            return "superseded"
        if code == 200 and data.get("outcome") == "denied":
            await self._ephemeral(p.channel_id, p.slack_user_id, REFUSAL.get(str(data.get("denial_reason")),
                                  f"You can't decide this gate ({data.get('denial_reason')})."))
            await self.repo.finish_pending(p.decision_id, "denied", data)
            return "denied"
        if code == 202:
            if not retry:
                await self._ephemeral(p.channel_id, p.slack_user_id,
                                      "Recording your decision (Replicant is catching up). You don't need to click again.")
            await self.repo.finish_pending(p.decision_id, "retry", None)
            return "recording"
        c = str(data.get("code") or "")
        if code in (409, 410) and c in ("gate_already_decided", "gate_closed", "job_closed"):
            wd = data.get("winning_decision") or {}
            line = _already(wd) if wd else "This gate is closed"
            await self._resolve_message(p.channel_id, p.message_ts, line, "closed")
            await self._ephemeral(p.channel_id, p.slack_user_id, f"Not recorded: {line}.")
            await self.repo.finish_pending(p.decision_id, "rejected", data)
            return "already_decided"
        if code == 409 and c == "stale_approval_view":
            await self._ephemeral(p.channel_id, p.slack_user_id, "This approval request changed since this message was "
                                  "posted. Review the new revision in Replicant, then decide again.")
            await self.repo.finish_pending(p.decision_id, "rejected", data)
            return "stale"
        if code in (401, 403, 404, 422):
            await self._ephemeral(p.channel_id, p.slack_user_id, REFUSAL.get(c, f"Not recorded ({c or code})."))
            await self.repo.finish_pending(p.decision_id, "rejected" if code != 403 else "denied", data)
            return "refused"
        await self.repo.finish_pending(p.decision_id, "retry", None)     # control-plane-api down: retried
        return "retry"

    async def retry_due(self) -> int:
        n = 0
        for p in await self.repo.due_pending():
            who = await self.repo.principal_for_slack(p.slack_user_id, self.team_id())
            await self.decide(p, who[1] if who else None, retry=True)
            n += 1
        return n

    # ------------------------------------------------------------------ message updates
    async def _resolve_message(self, channel: str, ts: str, line: str, final: str) -> None:
        """Re-render one posted gate message (if we know it) without its decision buttons."""
        if not channel or not ts:
            return
        refs = [r for r in await self._refs_for(channel, ts)]
        blocks = refs[0].blocks if refs else None
        try:
            await self.slack.update_message(channel=channel, ts=ts, text=line, blocks=resolved_blocks(blocks, line))
            await self.repo.set_final(channel, ts, final)
        except SlackFailure as e:
            _log("message_update_failed", channel=channel, code=e.code)

    async def _refs_for(self, channel: str, ts: str) -> list[Any]:
        getter = getattr(self.repo, "ref_by_message", None)
        if getter is None:
            return []
        r = await getter(channel, ts)
        return [r] if r else []

    async def gate_resolved(self, gate_id: str, final_state: str, line: str) -> int:
        """notify.gate_resolved -> every Slack message of the gate loses its buttons (idempotent per message)."""
        n = 0
        for ref in await self.repo.message_refs(gate_id):
            if ref.final_state:
                continue
            try:
                await self.slack.update_message(channel=ref.channel_id, ts=ref.message_ts, text=line,
                                                blocks=resolved_blocks(ref.blocks, line))
                await self.repo.set_final(ref.channel_id, ref.message_ts, final_state)
                n += 1
            except SlackFailure as e:
                _log("message_update_failed", channel=ref.channel_id, code=e.code)
                if e.kind != "permanent":
                    raise
        return n

    async def _ephemeral(self, channel: str, user: str, text: str) -> None:
        if not channel or not user:
            return
        try:
            await self.slack.post_ephemeral(channel=channel, user=user, text=text)
        except SlackFailure as e:
            _log("ephemeral_failed", code=e.code)


def _already(wd: dict[str, Any]) -> str:
    d = wd.get("decision") or "decided"
    verb = {"approve": "approved", "reject": "rejected", "request_changes": "changes requested"}.get(d, d)
    ch = wd.get("channel")
    where = {"ui": "in the UI", "cli": "from the CLI", "api": "by API", "slack": "in Slack"}.get(ch or "", f"via {ch}" if ch else "")
    return f"Already {verb} {where}".strip()
