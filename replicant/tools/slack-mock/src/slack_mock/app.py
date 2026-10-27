"""slack-mock: the Slack Web API subset Replicant uses, plus an inspection API and a viewer page (parity P-14).

Web API (`POST|GET /api/<method>`, JSON, form or query bodies, `Authorization: Bearer <bot token>` or a `token`
field): auth.test, chat.postMessage, chat.update, chat.postEphemeral, users.info, conversations.info,
conversations.open, views.open, views.publish. Answers are Slack-shaped (`{"ok": true, ...}` / `{"ok": false, "error": ...}`).

Inspection: `GET /_mock/messages[?channel=&user=&since_ts=&method=]`, `DELETE /_mock/messages`,
`POST|DELETE /_mock/faults` (inject `ratelimited` etc. for the next N calls of a method),
`POST /_mock/send/{interactions|commands|events}` (sign and forward a payload to slack-integration),
`POST /_mock/click` (viewer button, or JSON {channel, ts|record_id, action_id, user}: signed `block_actions`),
`POST /_mock/submit` (submit the last modal: signed `view_submission`), `GET /_mock/home/{user}` (App Home), `GET /healthz`, `GET /` (viewer).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from slack_mock.render import render_page

log = logging.getLogger("slack_mock")

JSON_FIELDS = ("blocks", "attachments", "metadata", "view")
MAX_TEXT = 40_000
MAX_BLOCKS = 50


@dataclass
class Workspace:
    team: dict[str, Any]
    users: dict[str, dict[str, Any]]
    channels: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: str) -> "Workspace":
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Workspace":
        team = dict(raw.get("team") or {"id": "T0LOCAL01", "name": "local", "domain": "local"})
        users = {u["id"]: dict(u) for u in raw.get("users") or []}
        channels = {c["id"]: dict(c) for c in raw.get("channels") or []}
        return cls(team=team, users=users, channels=channels)

    @property
    def bot_user_id(self) -> str:
        return next((u for u, d in self.users.items() if d.get("is_bot")), "U0BOTUSER")

    @staticmethod
    def dm_id(user_id: str) -> str:
        return "D" + user_id[1:]

    def dm_user(self, channel_id: str) -> str | None:
        """The workspace (non-bot) user a `D…` channel belongs to, or None."""
        if not channel_id.startswith("D"):
            return None
        uid = "U" + channel_id[1:]
        u = self.users.get(uid)
        return uid if u and not u.get("is_bot") else None

    def resolve_channel(self, ref: str) -> tuple[str, str | None] | None:
        """channel id | #name | name | D… | U… (app DM) -> (channel_id, dm_user)."""
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref in self.channels:
            return ref, None
        if ref.startswith("D"):
            u = self.dm_user(ref)
            return (ref, u) if u else None
        if ref.startswith(("U", "W")):
            u = self.users.get(ref)
            return (self.dm_id(ref), ref) if u and not u.get("is_bot") else None
        name = ref.lstrip("#")
        for cid, c in self.channels.items():
            if c.get("name") == name:
                return cid, None
        return None


@dataclass
class Store:
    records: list[dict[str, Any]] = field(default_factory=list)
    faults: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_click: dict[str, Any] | None = None
    _seq: int = 0
    _last_ts: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_ts(self) -> str:
        with self._lock:
            now = round(time.time(), 6)
            self._last_ts = now if now > self._last_ts else round(self._last_ts + 0.000001, 6)
            return f"{self._last_ts:.6f}"

    def record(self, *, method: str, channel: str, ts: str, user: str | None, payload: dict[str, Any],
               thread_ts: str | None = None, ephemeral_user: str | None = None) -> dict[str, Any]:
        event_ts = ts if method == "chat.postMessage" else self.next_ts()
        with self._lock:
            self._seq += 1
            rec = {"id": self._seq, "method": method, "channel": channel, "ts": ts, "event_ts": event_ts,
                   "user": user, "thread_ts": thread_ts, "ephemeral_user": ephemeral_user,
                   "text": payload.get("text"), "blocks": payload.get("blocks"), "payload": payload,
                   "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self.records.append(rec)
            return rec

    def find_message(self, channel: str, ts: str) -> dict[str, Any] | None:
        """Latest version (post or update) of the message (channel, ts)."""
        for r in reversed(self.records):
            if r["channel"] == channel and r["ts"] == ts and r["method"] in ("chat.postMessage", "chat.update"):
                return r
        return None

    def query(self, channel: str | None = None, user: str | None = None, since_ts: str | None = None,
              method: str | None = None) -> list[dict[str, Any]]:
        out = []
        since = float(since_ts) if since_ts else None
        with self._lock:
            for r in self.records:
                if channel and r["channel"] != channel:
                    continue
                if user and user not in (r["user"], r["ephemeral_user"]):
                    continue
                if method and r["method"] != method:
                    continue
                if since is not None and float(r["event_ts"]) <= since:
                    continue
                out.append(r)
        return out

    def take_fault(self, method: str) -> dict[str, Any] | None:
        with self._lock:
            f = self.faults.get(method) or self.faults.get("*")
            if not f:
                return None
            f["count"] -= 1
            if f["count"] <= 0:
                self.faults = {k: v for k, v in self.faults.items() if v is not f}
            return f


def sign(secret: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    base = b"v0:" + str(ts).encode() + b":" + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig}


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float = 5.0) -> dict[str, Any]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "body": r.read(65536).decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read(65536).decode("utf-8", "replace")}
    except Exception as e:  # noqa: BLE001 - surfaced to the caller of the helper
        return {"status": 0, "error": f"{type(e).__name__}: {e}"}


def _ok(**kw: Any) -> JSONResponse:
    return JSONResponse({"ok": True, **kw})


def _err(error: str, status: int = 200, headers: dict[str, str] | None = None, **kw: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error, **kw}, status_code=status, headers=headers)


async def _args(request: Request) -> dict[str, Any]:
    """Merge query, form and JSON bodies the way Slack does; JSON-encoded form fields (blocks, ...) are decoded."""
    args: dict[str, Any] = dict(request.query_params)
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw = await request.body()
    if raw:
        if ctype == "application/json":
            try:
                body = json.loads(raw)
            except ValueError:
                raise _BadRequest("invalid_json")
            if not isinstance(body, dict):
                raise _BadRequest("invalid_json")
            args.update(body)
        else:
            args.update(dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)))
    for k in JSON_FIELDS:
        if isinstance(args.get(k), str) and args[k]:
            try:
                args[k] = json.loads(args[k])
            except ValueError:
                raise _BadRequest(f"invalid_{k}" if k != "metadata" else "invalid_metadata_format")
    return args


class _BadRequest(Exception):
    pass


def _boolish(v: Any) -> bool:
    return v is True or str(v).lower() in ("1", "true", "yes")


def create_app(workspace: Workspace, *, bot_token: str | None, signing_secret: str | None = None,
               interactivity_url: str | None = None, commands_url: str | None = None,
               events_url: str | None = None, public_url: str = "http://slack-mock:8410") -> FastAPI:
    store = Store()
    app = FastAPI(title="slack-mock", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.workspace = workspace
    ws = workspace
    targets = {"interactions": interactivity_url, "commands": commands_url, "events": events_url}

    def user_obj(uid: str) -> dict[str, Any]:
        u = ws.users[uid]
        name = u.get("name", uid)
        real = u.get("real_name") or name.capitalize()
        prof = {"real_name": real, "display_name": name, "real_name_normalized": real, "display_name_normalized": name}
        if u.get("email"):
            prof["email"] = u["email"]
        return {"id": uid, "team_id": ws.team.get("id"), "name": name, "real_name": real, "deleted": False,
                "is_bot": bool(u.get("is_bot")), "is_admin": False, "tz": "Etc/UTC", "profile": prof}

    def channel_obj(cid: str, dm_user: str | None) -> dict[str, Any]:
        if dm_user:
            return {"id": cid, "is_im": True, "is_channel": False, "is_private": True, "user": dm_user,
                    "created": 1792800000, "is_open": True}
        c = ws.channels[cid]
        return {"id": cid, "name": c.get("name"), "is_channel": True, "is_im": False, "is_private": bool(c.get("is_private")),
                "is_archived": False, "is_member": True, "created": 1792800000}

    def message_obj(args: dict[str, Any], ts: str) -> dict[str, Any]:
        m = {"type": "message", "user": ws.bot_user_id, "bot_id": "B0LOCALBOT", "ts": ts, "text": args.get("text") or ""}
        for k in ("blocks", "metadata", "thread_ts", "attachments"):
            if args.get(k) is not None:
                m[k] = args[k]
        return m

    def check_content(args: dict[str, Any]) -> str | None:
        blocks, text = args.get("blocks"), args.get("text")
        if blocks is not None:
            if not isinstance(blocks, list) or not all(isinstance(b, dict) and b.get("type") for b in blocks):
                return "invalid_blocks"
            if len(blocks) > MAX_BLOCKS:
                return "invalid_blocks"
        if not text and not blocks and not args.get("attachments"):
            return "no_text"
        if text and len(str(text)) > MAX_TEXT:
            return "msg_too_long"
        return None

    # --- Web API ------------------------------------------------------------------------------------------------
    async def auth_test(args: dict[str, Any]) -> JSONResponse:
        team = ws.team
        return _ok(url=f"https://{team.get('domain', 'local')}.slack.com/", team=team.get("name"), user="replicant",
                   team_id=team.get("id"), user_id=ws.bot_user_id, bot_id="B0LOCALBOT", is_enterprise_install=False)

    async def post_message(args: dict[str, Any]) -> JSONResponse:
        target = ws.resolve_channel(str(args.get("channel") or ""))
        if target is None:
            return _err("channel_not_found")
        if (e := check_content(args)) is not None:
            return _err(e)
        cid, dm_user = target
        ts = store.next_ts()
        store.record(method="chat.postMessage", channel=cid, ts=ts, user=dm_user, payload=args,
                     thread_ts=args.get("thread_ts"))
        return _ok(channel=cid, ts=ts, message=message_obj(args, ts))

    async def update(args: dict[str, Any]) -> JSONResponse:
        target = ws.resolve_channel(str(args.get("channel") or ""))
        if target is None:
            return _err("channel_not_found")
        cid, dm_user = target
        ts = str(args.get("ts") or "")
        prev = store.find_message(cid, ts) if ts else None
        if prev is None:
            return _err("message_not_found")
        if (e := check_content({**args, "text": args.get("text") or prev.get("text")})) is not None:
            return _err(e)
        store.record(method="chat.update", channel=cid, ts=ts, user=dm_user, payload=args, thread_ts=prev.get("thread_ts"))
        return _ok(channel=cid, ts=ts, text=args.get("text") or "", message=message_obj(args, ts))

    async def post_ephemeral(args: dict[str, Any]) -> JSONResponse:
        target = ws.resolve_channel(str(args.get("channel") or ""))
        if target is None:
            return _err("channel_not_found")
        uid = str(args.get("user") or "")
        if uid not in ws.users:
            return _err("user_not_found")
        if (e := check_content(args)) is not None:
            return _err(e)
        cid, dm_user = target
        ts = store.next_ts()
        store.record(method="chat.postEphemeral", channel=cid, ts=ts, user=dm_user, payload=args, ephemeral_user=uid,
                     thread_ts=args.get("thread_ts"))
        return _ok(message_ts=ts)

    async def users_info(args: dict[str, Any]) -> JSONResponse:
        uid = str(args.get("user") or "")
        if uid not in ws.users:
            return _err("user_not_found")
        return _ok(user=user_obj(uid))

    async def conversations_info(args: dict[str, Any]) -> JSONResponse:
        ref = str(args.get("channel") or "")
        target = ws.resolve_channel(ref) if not ref.startswith(("U", "W")) else None
        if target is None:
            return _err("channel_not_found")
        return _ok(channel=channel_obj(*target))

    async def conversations_open(args: dict[str, Any]) -> JSONResponse:
        if args.get("channel"):
            ch = str(args["channel"])
            if ws.dm_user(ch) is None:
                return _err("channel_not_found")
            return _ok(channel={"id": ch}, already_open=True)
        users = args.get("users")
        ids = [u.strip() for u in (users if isinstance(users, list) else str(users or "").split(",")) if u.strip()]
        if not ids:
            return _err("users_list_not_supplied")
        if len(ids) > 1:
            return _err("not_supported", detail="slack-mock opens 1:1 DMs only")
        uid = ids[0]
        u = ws.users.get(uid)
        if u is None:
            return _err("user_not_found")
        if u.get("is_bot"):
            return _err("cannot_dm_bot")
        cid = ws.dm_id(uid)
        if _boolish(args.get("return_im")):
            return _ok(channel=channel_obj(cid, uid), already_open=False)
        return _ok(channel={"id": cid}, already_open=False)

    async def views_open(args: dict[str, Any]) -> JSONResponse:
        if not args.get("trigger_id") and not args.get("interactivity_pointer"):
            return _err("invalid_arguments", response_metadata={"messages": ["[ERROR] missing required field: trigger_id"]})
        view = args.get("view")
        if not isinstance(view, dict) or view.get("type") not in ("modal", "home"):
            return _err("invalid_arguments", response_metadata={"messages": ["[ERROR] invalid view"]})
        ts = store.next_ts()
        vid = "V" + hashlib.sha1(ts.encode()).hexdigest()[:10].upper()
        store.record(method="views.open", channel="", ts=ts, user=None, payload=args)
        return _ok(view={"id": vid, "team_id": ws.team.get("id"), **view})

    async def views_publish(args: dict[str, Any]) -> JSONResponse:
        uid = str(args.get("user_id") or "")
        if uid not in ws.users or ws.users[uid].get("is_bot"):
            return _err("user_not_found")
        view = args.get("view")
        if not isinstance(view, dict) or view.get("type") != "home":
            return _err("invalid_arguments", response_metadata={"messages": ["[ERROR] view.type must be home"]})
        ts = store.next_ts()
        store.record(method="views.publish", channel=ws.dm_id(uid), ts=ts, user=uid, payload=args)
        return _ok(view={"id": "VHOME" + uid[1:], "team_id": ws.team.get("id"), **view})

    methods = {"views.publish": views_publish, "auth.test": auth_test, "chat.postMessage": post_message, "chat.update": update,
               "chat.postEphemeral": post_ephemeral, "users.info": users_info, "conversations.info": conversations_info,
               "conversations.open": conversations_open, "views.open": views_open}

    @app.api_route("/api/{method}", methods=["GET", "POST"])
    async def web_api(method: str, request: Request) -> Response:
        handler = methods.get(method)
        if handler is None:
            return _err("unknown_method", status=404, req_method=method)
        try:
            args = await _args(request)
        except _BadRequest as e:
            return _err(str(e))
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h[:7].lower() == "bearer " else str(args.pop("token", "") or "")
        args.pop("token", None)
        if not tok:
            return _err("not_authed")
        if not bot_token or not hmac.compare_digest(tok.encode(), bot_token.encode()):
            return _err("invalid_auth")
        fault = store.take_fault(method)
        if fault is not None:
            hdrs = {"Retry-After": str(fault["retry_after"])} if fault.get("retry_after") is not None else None
            return _err(fault["error"], status=int(fault.get("status") or 200), headers=hdrs)
        resp = await handler(args)
        log.info(json.dumps({"msg": "api_call", "method": method, "ok": json.loads(resp.body).get("ok")}))
        return resp

    # --- inspection API -----------------------------------------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "team_id": ws.team.get("id"), "messages": len(store.records)}

    @app.get("/_mock/messages")
    async def list_messages(channel: str | None = None, user: str | None = None, since_ts: str | None = None,
                            method: str | None = None) -> Response:
        try:
            msgs = store.query(channel=channel, user=user, since_ts=since_ts, method=method)
        except ValueError:
            return JSONResponse({"error": "invalid since_ts"}, status_code=400)
        return JSONResponse({"messages": msgs})

    @app.delete("/_mock/messages")
    async def clear_messages() -> Response:
        with store._lock:
            store.records.clear()
        return Response(status_code=204)

    @app.post("/_mock/faults")
    async def add_fault(request: Request) -> Response:
        try:
            body = await request.json()
            method, error = str(body["method"]), str(body["error"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "body must be {method, error, count?, status?, retry_after?}"}, status_code=400)
        status = int(body.get("status") or (429 if error == "ratelimited" else 200))
        retry = body.get("retry_after", 1 if error == "ratelimited" else None)
        store.faults[method] = {"error": error, "count": int(body.get("count") or 1), "status": status, "retry_after": retry}
        return JSONResponse({"faults": store.faults})

    @app.delete("/_mock/faults")
    async def clear_faults() -> Response:
        store.faults.clear()
        return Response(status_code=204)

    async def forward(kind: str, body: bytes, ctype: str) -> dict[str, Any]:
        url = targets.get(kind)
        if not url:
            return {"status": 0, "error": f"no target URL configured for {kind}"}
        if not signing_secret:
            return {"status": 0, "error": "no signing secret configured"}
        headers = {"Content-Type": ctype, **sign(signing_secret, body)}
        return await asyncio.to_thread(_post, url, body, headers)

    @app.post("/_mock/send/{kind}")
    async def send(kind: str, request: Request) -> Response:
        """interactions: {"payload": {...}} -> form `payload=<json>`; commands: {"form": {...}}; events: raw JSON."""
        if kind not in targets:
            return JSONResponse({"error": "kind must be interactions|commands|events"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if kind == "interactions":
            raw = urllib.parse.urlencode({"payload": json.dumps(body.get("payload", body))}).encode()
            ctype = "application/x-www-form-urlencoded"
        elif kind == "commands":
            raw = urllib.parse.urlencode(body.get("form", body)).encode()
            ctype = "application/x-www-form-urlencoded"
        else:
            raw, ctype = json.dumps(body).encode(), "application/json"
        return JSONResponse(await forward(kind, raw, ctype))

    @app.post("/_mock/response/{record_id}")
    async def response_url(record_id: int, request: Request) -> Response:
        """Stand-in for a block_actions `response_url`: recorded as method `response_url`."""
        try:
            args = await _args(request)
        except _BadRequest as e:
            return _err(str(e), status=400)
        src = next((r for r in store.records if r["id"] == record_id), None)
        if src is None:
            return _err("expired_url", status=404)
        if _boolish(args.get("replace_original")) and src["method"] in ("chat.postMessage", "chat.update"):
            store.record(method="chat.update", channel=src["channel"], ts=src["ts"], user=src["user"], payload=args)
        else:
            store.record(method="response_url", channel=src["channel"], ts=store.next_ts(), user=src["user"],
                         payload=args)
        return _ok()

    @app.get("/_mock/home/{user}")
    async def home(user: str) -> Response:
        recs = store.query(user=user, method="views.publish")
        return JSONResponse({"view": recs[-1]["payload"].get("view") if recs else None, "published": len(recs)})

    @app.post("/_mock/submit")
    async def submit(request: Request) -> Response:
        """Submit the latest modal opened with views.open: {user, values: {block_id: {action_id: value}}}."""
        body = await request.json()
        uid = str(body.get("user") or "")
        opened = [r for r in store.records if r["method"] == "views.open"]
        if not opened or uid not in ws.users:
            return JSONResponse({"error": "no modal open or unknown user"}, status_code=404)
        view = dict(opened[-1]["payload"].get("view") or {})
        values = {bid: {aid: {"type": "plain_text_input", "value": v} for aid, v in a.items()}
                  for bid, a in (body.get("values") or {}).items()}
        view.update({"id": "V" + hashlib.sha1(opened[-1]["ts"].encode()).hexdigest()[:10].upper(),
                     "state": {"values": values}})
        payload = {"type": "view_submission", "team": {"id": ws.team.get("id")},
                   "user": {"id": uid, "name": ws.users[uid].get("name"), "team_id": ws.team.get("id")}, "view": view}
        raw = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
        return JSONResponse(await forward("interactions", raw, "application/x-www-form-urlencoded"))

    @app.post("/_mock/click")
    async def click(request: Request) -> Response:
        as_json = (request.headers.get("content-type") or "").startswith("application/json")
        if as_json:
            jb = await request.json()
            form = {k: str(v) for k, v in jb.items() if v is not None}
            if "record_id" not in form and form.get("channel") and form.get("ts"):
                m = store.find_message(form["channel"], form["ts"])
                form["record_id"] = str(m["id"]) if m else "0"
        else:
            form = dict(urllib.parse.parse_qsl((await request.body()).decode("utf-8", "replace")))
        try:
            rec = next(r for r in store.records if r["id"] == int(form.get("record_id", "0")))
        except (StopIteration, ValueError):
            return JSONResponse({"error": "no such message"}, status_code=404) if as_json else \
                RedirectResponse("/", status_code=303)
        action = None
        block_id = None
        for b in rec.get("blocks") or []:
            if b.get("type") != "actions":
                continue
            for el in b.get("elements") or []:
                if el.get("action_id") == form.get("action_id"):
                    action, block_id = el, b.get("block_id")
        uid = form.get("user") or rec.get("user") or next((u for u, d in ws.users.items() if not d.get("is_bot")), "")
        if action is None or uid not in ws.users:
            return JSONResponse({"error": "no such action or user"}, status_code=404) if as_json else \
                RedirectResponse("/", status_code=303)
        ts = store.next_ts()
        a = {"type": action.get("type", "button"), "action_id": action.get("action_id"), "block_id": block_id,
             "text": action.get("text"), "action_ts": ts}
        for k in ("value", "url", "style"):
            if action.get(k) is not None:
                a[k] = action[k]
        team = {"id": ws.team.get("id"), "domain": ws.team.get("domain")}
        channel = {"id": rec["channel"], "name": "directmessage" if rec.get("user") else
                   ws.channels.get(rec["channel"], {}).get("name")}
        payload = {"type": "block_actions", "team": team, "api_app_id": "A0LOCALAPP",
                   "user": {"id": uid, "username": ws.users[uid].get("name"), "name": ws.users[uid].get("name"),
                            "team_id": team["id"]},
                   "container": {"type": "message", "message_ts": rec["ts"], "channel_id": rec["channel"],
                                 "is_ephemeral": False},
                   "trigger_id": f"{int(time.time())}.{abs(hash(ts)) % 10**12}.mock",
                   "channel": channel, "message": message_obj(rec["payload"], rec["ts"]),
                   "response_url": f"{public_url.rstrip('/')}/_mock/response/{rec['id']}",
                   "actions": [a]}
        raw = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
        result = await forward("interactions", raw, "application/x-www-form-urlencoded")
        store.last_click = {"action_id": a["action_id"], "user": uid, "record_id": rec["id"], **result}
        return JSONResponse(store.last_click) if as_json else RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def index(channel: str | None = None, user: str | None = None, refresh: int = 5) -> HTMLResponse:
        msgs = list(reversed(store.query(channel=channel, user=user)))
        return HTMLResponse(render_page(ws, msgs, last_click=store.last_click, refresh=refresh,
                                        clicks_enabled=bool(interactivity_url and signing_secret)))

    return app
