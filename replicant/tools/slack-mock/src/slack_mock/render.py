"""Rough HTML rendering of recorded messages (mrkdwn + the Block Kit blocks Replicant sends) for the viewer page."""
from __future__ import annotations

import html
import re
from typing import Any

_TOKEN = re.compile(r"<([^<>]+)>")
_BOLD = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_ITAL = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_STRIKE = re.compile(r"(?<![\w~])~([^~\n]+)~(?![\w~])")
_CODE = re.compile(r"`([^`\n]+)`")


def _plain(s: str) -> str:
    """Slack-escaped text -> HTML-escaped text with inline mrkdwn styles."""
    s = html.escape(s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"), quote=False)
    s = _CODE.sub(r"<code>\1</code>", s)
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _ITAL.sub(r"<i>\1</i>", s)
    s = _STRIKE.sub(r"<s>\1</s>", s)
    return s


def mrkdwn(text: str | None, users: dict[str, dict[str, Any]] | None = None) -> str:
    if not text:
        return ""
    users = users or {}
    out: list[str] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        out.append(_plain(text[pos:m.start()]))
        tok = m.group(1)
        target, _, label = tok.partition("|")
        if target.startswith("@"):
            uid = target[1:]
            out.append(f'<span class="mention">@{html.escape(users.get(uid, {}).get("name", uid))}</span>')
        elif target.startswith("#"):
            out.append(f'<span class="mention">#{html.escape(label or target[1:])}</span>')
        elif target.startswith("!"):
            out.append(_plain(label or target[1:]))
        else:
            href = target.replace("&amp;", "&")
            safe = href if href.startswith(("http://", "https://", "mailto:")) else "#"
            out.append(f'<a href="{html.escape(safe)}" target="_blank" rel="noopener">{_plain(label or target)}</a>')
        pos = m.end()
    out.append(_plain(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def _text_obj(t: dict[str, Any] | None, users: dict[str, dict[str, Any]]) -> str:
    if not t:
        return ""
    if t.get("type") == "mrkdwn":
        return mrkdwn(t.get("text"), users)
    return html.escape(t.get("text") or "")


def render_blocks(rec: dict[str, Any], users: dict[str, dict[str, Any]], clicks_enabled: bool) -> str:
    parts: list[str] = []
    for b in rec.get("blocks") or []:
        t = b.get("type")
        if t == "header":
            parts.append(f'<div class="b-header">{_text_obj(b.get("text"), users)}</div>')
        elif t == "section":
            body = f'<div>{_text_obj(b.get("text"), users)}</div>' if b.get("text") else ""
            if b.get("fields"):
                body += '<div class="fields">' + "".join(
                    f'<div>{_text_obj(f, users)}</div>' for f in b["fields"]) + "</div>"
            parts.append(f'<div class="b-section">{body}</div>')
        elif t == "context":
            parts.append('<div class="b-context">' + " · ".join(
                _text_obj(e, users) for e in b.get("elements") or [] if e.get("type") in ("mrkdwn", "plain_text")) + "</div>")
        elif t == "divider":
            parts.append("<hr>")
        elif t == "actions":
            btns = []
            for e in b.get("elements") or []:
                if e.get("type") != "button":
                    continue
                cls = "btn " + (e.get("style") or "")
                label = html.escape((e.get("text") or {}).get("text") or "")
                if e.get("url") and str(e["url"]).startswith(("http://", "https://")):
                    btns.append(f'<a class="{cls}" href="{html.escape(e["url"])}" target="_blank" rel="noopener">{label} ↗</a>')
                elif clicks_enabled:
                    btns.append(
                        f'<form method="post" action="/_mock/click"><input type="hidden" name="record_id" '
                        f'value="{rec["id"]}"><input type="hidden" name="action_id" '
                        f'value="{html.escape(str(e.get("action_id") or ""))}"><button class="{cls}">{label}</button></form>')
                else:
                    btns.append(f'<span class="{cls} disabled">{label}</span>')
            parts.append('<div class="b-actions">' + "".join(btns) + "</div>")
        else:
            parts.append(f'<div class="b-other">[{html.escape(str(t))} block]</div>')
    return "".join(parts)


CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1c1d;--mute:#616061;--line:#e2e0dc;--accent:#1264a3;--primary:#007a5a;--danger:#e01e5a}
@media (prefers-color-scheme: dark){:root{--bg:#1a1d21;--card:#222529;--ink:#e8e8e8;--mute:#a0a0a0;--line:#35373b;--accent:#4aa3df}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mute);font-size:13px;margin-bottom:16px}
.msg{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 12px}
.meta{color:var(--mute);font-size:12px;margin-bottom:6px;display:flex;flex-wrap:wrap;gap:8px}.meta b{color:var(--ink)}
.tag{border:1px solid var(--line);border-radius:4px;padding:0 5px}.b-header{font-weight:700;font-size:17px;margin:4px 0}
.b-section{margin:6px 0}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px 16px;margin-top:6px}
.b-context{color:var(--mute);font-size:13px;margin:6px 0}.b-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.b-actions form{margin:0}.btn{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:4px 12px;background:var(--card);
color:var(--ink);text-decoration:none;font:inherit;font-size:14px;cursor:pointer}.btn.primary{background:var(--primary);color:#fff;border-color:var(--primary)}
.btn.danger{color:var(--danger);border-color:var(--danger)}.btn.disabled{opacity:.6;cursor:default}
a{color:var(--accent)}code{background:rgba(127,127,127,.15);border-radius:3px;padding:0 3px}.mention{color:var(--accent);font-weight:600}
.flash{background:var(--card);border-left:4px solid var(--accent);padding:8px 12px;margin-bottom:12px;font-size:13px;word-break:break-word}
.fallback{color:var(--mute);font-size:12px;margin-top:6px}.empty{color:var(--mute)}pre{white-space:pre-wrap;word-break:break-word}
"""


def render_page(ws: Any, msgs: list[dict[str, Any]], *, last_click: dict[str, Any] | None, refresh: int,
                clicks_enabled: bool) -> str:
    users = ws.users
    items = []
    for r in msgs:
        who = users.get(r["user"] or "", {}).get("name") if r.get("user") else None
        chan = f"DM @{who}" if who else "#" + str(ws.channels.get(r["channel"], {}).get("name", r["channel"] or "-"))
        meta = [f'<b>{html.escape(chan)}</b>', f'<span class="tag">{html.escape(r["method"])}</span>',
                f'ts {html.escape(r["ts"])}', html.escape(r["recorded_at"])]
        if r.get("ephemeral_user"):
            meta.append("only visible to @" + html.escape(users.get(r["ephemeral_user"], {}).get("name", r["ephemeral_user"])))
        if r.get("thread_ts"):
            meta.append("in thread " + html.escape(str(r["thread_ts"])))
        if r["method"] == "views.open":
            view = r["payload"].get("view") or {}
            body = f'<div class="b-header">{_text_obj(view.get("title"), users)} (modal)</div>' + render_blocks(
                {"id": r["id"], "blocks": view.get("blocks")}, users, False)
        elif r.get("blocks"):
            body = render_blocks(r, users, clicks_enabled)
            if r.get("text"):
                body += f'<div class="fallback">fallback text: {html.escape(str(r["text"]))}</div>'
        else:
            body = f'<div class="b-section">{mrkdwn(r.get("text"), users)}</div>'
        items.append(f'<div class="msg"><div class="meta">{" · ".join(meta)}</div>{body}</div>')
    flash = ""
    if last_click:
        flash = (f'<div class="flash">Last click: <code>{html.escape(str(last_click.get("action_id")))}</code> as '
                 f'@{html.escape(users.get(last_click.get("user"), {}).get("name", str(last_click.get("user"))))} → '
                 f'HTTP {html.escape(str(last_click.get("status")))} '
                 f'<pre>{html.escape(str(last_click.get("body") or last_click.get("error") or ""))[:600]}</pre></div>')
    head = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh > 0 else ""
    team = html.escape(str(ws.team.get("name", "")))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1">{head}<title>Slack mock</title><style>{CSS}</style></head>'
            f'<body><main><h1>Slack mock · {team}</h1><div class="sub">{len(msgs)} recorded messages, newest first. '
            f'Auto-refresh {int(refresh)} s (<a href="?refresh=0">off</a>). JSON: <a href="/_mock/messages">/_mock/messages</a></div>'
            f'{flash}{"".join(items) or "<p class=empty>No messages yet.</p>"}</main></body></html>')
