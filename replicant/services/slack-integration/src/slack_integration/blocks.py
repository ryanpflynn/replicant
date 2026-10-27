"""Notification DM rendering: header(title); section(text); section(fields); context; actions(URL buttons).

Block Kit limits respected (approvals.md §4.3): <= 50 blocks, header plain_text <= 150, section text <= 3,000,
<= 10 fields of <= 2,000, context text <= 3,000, button text <= 75, url <= 3,000, action_id <= 255, `primary` on one
button only. Over-long values are truncated (never rejected) without cutting a `<…>` link or an `&…;` entity in half.
`text`, field values and `context` arrive as mrkdwn from control-plane-api; `title` and field labels are plain text
and are escaped here when placed into mrkdwn.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DELIVERY_ID_RE = r"^ntf_[0-9a-hjkmnp-tv-z]{26}$"
HEADER_MAX = 150
SECTION_MAX = 3000
FIELD_MAX = 2000
FIELDS_MAX = 10
CONTEXT_MAX = 3000
BUTTONS_MAX = 5
BUTTON_TEXT_MAX = 75
URL_MAX = 3000
FALLBACK_MAX = 3000
_AMP = re.compile(r"&(?!(?:amp|lt|gt);)")


class NotifyField(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str = Field(max_length=500)
    value: str = Field(max_length=10_000)


class NotifyLink(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=10_000)
    primary: bool = False


class NotifyAction(BaseModel):
    """Interactive button (gate decisions, approvals.md §4.3). `value` is opaque JSON {j, g, r, d}."""
    model_config = ConfigDict(extra="ignore")
    action_id: str = Field(pattern=r"^gate_(approve|reject)$")
    label: str = Field(min_length=1, max_length=75)
    value: str = Field(min_length=2, max_length=2000)
    style: str | None = Field(default=None, pattern=r"^(primary|danger)$")
    confirm: dict[str, str] | None = None


class GateRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gate_id: str = Field(pattern=r"^gate_[0-9a-hjkmnp-tv-z]{26}$")
    revision: int = Field(ge=1)
    presented_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ThreadSpec(BaseModel):
    """Channel post threaded per job (NOTIFY-D13): the parent is posted on first use."""
    model_config = ConfigDict(extra="ignore")
    channel_id: str = Field(pattern=r"^C[A-Z0-9]{2,30}$")
    key: str = Field(min_length=1, max_length=128)
    parent: dict[str, Any] = Field(default_factory=dict)


class DeliverRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    delivery_id: str = Field(pattern=DELIVERY_ID_RE)
    event_key: str = Field(min_length=1, max_length=128)
    recipient: str = Field(min_length=1, max_length=128)
    tenant_id: str | None = Field(default=None, max_length=128)
    job_id: str | None = Field(default=None, max_length=128)
    gate_id: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=2000)
    text: str = Field(default="", max_length=40_000)
    fields: list[NotifyField] = Field(default_factory=list, max_length=50)
    context: str | None = Field(default=None, max_length=40_000)
    links: list[NotifyLink] = Field(default_factory=list, max_length=50)
    actions: list[NotifyAction] = Field(default_factory=list, max_length=4)
    gate: GateRef | None = None
    channel_id: str | None = Field(default=None, pattern=r"^C[A-Z0-9]{2,30}$")
    thread: ThreadSpec | None = None


def escape(s: str) -> str:
    """Slack mrkdwn escaping of `&`, `<`, `>` (an existing `&amp;`/`&lt;`/`&gt;` is left alone)."""
    return _AMP.sub("&amp;", s).replace("<", "&lt;").replace(">", "&gt;")


def truncate_plain(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def truncate_mrkdwn(s: str, n: int) -> str:
    """Truncate to <= n chars without leaving a dangling `<url|label` or a partial `&amp;` entity."""
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    lt, gt = cut.rfind("<"), cut.rfind(">")
    if lt > gt:
        cut = cut[:lt]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    return cut.rstrip() + "…"


def _mrkdwn(text: str) -> dict[str, str]:
    return {"type": "mrkdwn", "text": text}


def _plain(text: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": text, "emoji": True}


def build_blocks(req: DeliverRequest) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "block_id": "title", "text": _plain(truncate_plain(req.title, HEADER_MAX))}]
    if req.text.strip():
        blocks.append({"type": "section", "block_id": "body", "text": _mrkdwn(truncate_mrkdwn(req.text, SECTION_MAX))})
    fields = []
    for f in req.fields[:FIELDS_MAX]:
        label = escape(truncate_plain(f.label, 200)) if f.label.strip() else ""
        txt = f"*{label}*\n{f.value}" if label else f.value
        if txt.strip():
            fields.append(_mrkdwn(truncate_mrkdwn(txt, FIELD_MAX)))
    if fields:
        blocks.append({"type": "section", "block_id": "fields", "fields": fields})
    if req.context and req.context.strip():
        blocks.append({"type": "context", "block_id": "context",
                       "elements": [_mrkdwn(truncate_mrkdwn(req.context, CONTEXT_MAX))]})
    buttons: list[dict[str, Any]] = []
    primary_used = False
    for a in req.actions:
        b = {"type": "button", "action_id": a.action_id, "text": _plain(truncate_plain(a.label, BUTTON_TEXT_MAX)),
             "value": a.value}
        if a.style and not (a.style == "primary" and primary_used):
            b["style"] = a.style
            primary_used = primary_used or a.style == "primary"
        if a.confirm:
            b["confirm"] = {"title": _plain(a.confirm.get("title", "Are you sure?")[:100]),
                            "text": _mrkdwn(a.confirm.get("text", "")[:300] or " "),
                            "confirm": _plain(a.confirm.get("confirm", "Confirm")[:30]),
                            "deny": _plain(a.confirm.get("deny", "Back")[:30])}
        buttons.append(b)
    for link in req.links:
        if len(buttons) >= BUTTONS_MAX:
            break
        url = link.url.strip()
        if not url.lower().startswith(("http://", "https://")) or len(url) > URL_MAX:
            continue
        b: dict[str, Any] = {"type": "button", "action_id": f"open_{len(buttons)}",
                             "text": _plain(truncate_plain(link.label, BUTTON_TEXT_MAX)), "url": url}
        if link.primary and not primary_used:
            b["style"] = "primary"
            primary_used = True
        buttons.append(b)
    if buttons:
        bid = f"decide-r{req.gate.revision}" if req.actions and req.gate else "links"
        blocks.append({"type": "actions", "block_id": bid, "elements": buttons})
    return blocks


def resolved_blocks(blocks: list[dict[str, Any]] | None, line: str, title: str = "Approval") -> list[dict[str, Any]]:
    """approvals.md §4.5: the message keeps its content; decision buttons are removed (URL buttons stay) and a context
    line states the outcome."""
    out: list[dict[str, Any]] = []
    for b in blocks or [{"type": "header", "block_id": "title", "text": _plain(title)}]:
        if b.get("type") == "actions":
            els = [e for e in b.get("elements") or [] if not str(e.get("action_id", "")).startswith("gate_")]
            if els:
                out.append({**b, "block_id": "links", "elements": els})
            continue
        if b.get("block_id") == "outcome":
            continue
        out.append(b)
    out.append({"type": "context", "block_id": "outcome", "elements": [_mrkdwn(truncate_mrkdwn(line, CONTEXT_MAX))]})
    return out[:50]


def reject_modal(value: str, channel_id: str, message_ts: str) -> dict[str, Any]:
    """Reject needs a comment (C-MSG); the modal carries the gate reference in private_metadata."""
    import json as _json
    meta = _json.dumps({"v": value, "c": channel_id, "t": message_ts}, separators=(",", ":"))
    return {"type": "modal", "callback_id": "gate_reject_modal", "private_metadata": meta[:3000],
            "title": _plain("Reject approval"), "submit": _plain("Reject"), "close": _plain("Back"),
            "blocks": [{"type": "input", "block_id": "comment",
                        "label": _plain("Why are you rejecting this change?"),
                        "element": {"type": "plain_text_input", "action_id": "text", "multiline": True,
                                    "min_length": 3, "max_length": 8000}}]}


def home_view(doc: dict[str, Any] | None, web_url: str, linked: bool) -> dict[str, Any]:
    """App Home tab: the same list as /waiting (approvals.md §4.6)."""
    blocks: list[dict[str, Any]] = [{"type": "header", "text": _plain("Waiting on you")}]
    if not linked:
        blocks.append({"type": "section", "text": _mrkdwn(
            "Link your Slack account to Replicant to see what is waiting on you.")})
        return {"type": "home", "blocks": blocks}
    items = (doc or {}).get("items") or []
    counts = (doc or {}).get("counts") or {}
    blocks.append({"type": "context", "elements": [_mrkdwn(
        f"{counts.get('gates', 0)} approvals · {counts.get('prs', 0)} pull requests · <{web_url}/waiting|open in Replicant>")]})
    if not items:
        blocks.append({"type": "section", "text": _mrkdwn("Nothing is waiting on you.")})
    for it in items[:40]:
        repo = escape(str(it.get("repo", "")).rsplit("/", 1)[-1])
        title = escape(truncate_plain(str(it.get("title", "")), 150))
        if it.get("kind") == "gate":
            url = f"{web_url}{it.get('href', '/waiting')}"
            label = str(it.get("gate_kind", "approval")).replace("_", " ").capitalize()
            txt = f"*<{url}|{label}>* · `{repo}` · {escape(str(it.get('risk_level', '')))} risk\n{title}"
        else:
            pr = it.get("pr") or {}
            url = pr.get("url") or f"{web_url}{it.get('href', '/waiting')}"
            txt = f"*<{url}|PR #{pr.get('number')}>* · `{repo}` · you are the {escape(str(it.get('role', '')))}\n{title}"
        blocks.append({"type": "section", "text": _mrkdwn(truncate_mrkdwn(txt, SECTION_MAX))})
    return {"type": "home", "blocks": blocks[:100]}


def fallback_text(req: DeliverRequest) -> str:
    """Top-level `text` (notification and screen-reader fallback; mrkdwn)."""
    title = escape(truncate_plain(req.title, HEADER_MAX))
    body = req.text.strip()
    return truncate_mrkdwn(f"{title}\n{body}" if body else title, FALLBACK_MAX)


def thread_parent_blocks(parent: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    title = truncate_plain(str(parent.get("title") or "Replicant job"), HEADER_MAX)
    text = str(parent.get("text") or "")
    blocks: list[dict[str, Any]] = [{"type": "header", "block_id": "title", "text": _plain(title)}]
    if text:
        blocks.append({"type": "section", "block_id": "body", "text": _mrkdwn(truncate_mrkdwn(text, SECTION_MAX))})
    links = [lk for lk in parent.get("links") or [] if str(lk.get("url", "")).startswith(("http://", "https://"))]
    if links:
        blocks.append({"type": "actions", "block_id": "links", "elements": [
            {"type": "button", "action_id": f"open_{i}", "text": _plain(truncate_plain(str(lk.get("label", "Open")), 75)),
             "url": lk["url"]} for i, lk in enumerate(links[:5])]})
    return truncate_mrkdwn(f"{escape(title)}\n{text}", FALLBACK_MAX), blocks


def metadata(req: DeliverRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {"delivery_id": req.delivery_id, "event_key": req.event_key}
    if req.job_id:
        payload["job_id"] = req.job_id
    if req.gate_id:
        payload["gate_id"] = req.gate_id
    return {"event_type": "replicant_notification", "event_payload": payload}
