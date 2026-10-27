"""Agent-activity feed for one run (GRAPH-*, docs/v0/DEVIATIONS.md): parses the run's spooled transcript chunks
(C-ART `transcripts` bucket, `<run prefix>{cli_events,transcript,sandbox_log}/part-NNNNN.ndjson.zst`, written by
replicant-entrypoint and shipped by the uploader sidecar after agentd's known-credential redaction) into a
display feed: assistant messages, tool calls with their inputs (Bash commands, file paths, Edit diffs), truncated tool
results, usage, steering, the final result and QA/bootstrap log lines.

Pure functions, no I/O: routes_graph.py fetches the chunks. Every surfaced string goes through the WS-08 source-side
redactor (replicant_telemetry.redact) plus a JWT mask, on top of the uploader's stage-1 redaction.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any, Iterable

MAX_TEXT = 8000          # assistant text
MAX_INPUT = 4000         # tool input preview
MAX_RESULT = 4000        # tool result text
MAX_DIFF = 16000         # unified diff for Edit/Write
MAX_LOG_LINES = 400      # sandbox_log lines kept (tail)
MAX_ITEMS = 3000         # feed items returned (tail)

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_BEARER = re.compile(r"(?i)\b(bearer|authorization:)\s+[A-Za-z0-9._~+/=-]{16,}")


def _redactor():  # noqa: ANN202 - lazy import: the redactor loads its rule files once
    try:
        from replicant_telemetry.redact import redact_text
        return redact_text
    except Exception:  # noqa: BLE001 - library absent: masks below still apply
        return lambda s: s


_redact_text = None


def redact(s: str) -> str:
    """WS-08 source-side rules (fail closed inside redact_text) + run-token masks. Never surfaces a bearer/JWT."""
    global _redact_text
    if not s:
        return s
    if _redact_text is None:
        _redact_text = _redactor()
    out = _redact_text(s)
    out = _JWT.sub("[REDACTED:jwt]", out)
    return _BEARER.sub(lambda m: m.group(1) + " [REDACTED:bearer]", out)


def clip(s: str, limit: int) -> tuple[str, bool]:
    s = redact(s)
    if len(s) <= limit:
        return s, False
    head = limit * 3 // 4
    tail = limit - head
    return s[:head] + f"\n…[{len(s) - limit} chars truncated]…\n" + s[-tail:], True


def parse_ndjson(body: bytes) -> list[dict[str, Any]]:
    out = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def unified_diff(path: str, old: str, new: str) -> str:
    p = _short_path(path).lstrip("/")
    d = difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile="a/" + p, tofile="b/" + p, lineterm="", n=2)
    return "\n".join(d)


def _short_path(p: str) -> str:
    for pre in ("/workspace/repo/", "/workspace/"):
        if p.startswith(pre):
            return p[len(pre):]
    return p


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif isinstance(c, dict) and c.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else json.dumps(content)[:MAX_RESULT]


def describe_tool(name: str, inp: dict[str, Any]) -> dict[str, Any]:
    """Display fields for one tool call: a one-line summary, the salient input, and a diff for edits."""
    out: dict[str, Any] = {}
    fp = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
    if fp:
        out["path"] = _short_path(fp)
    if name == "Bash":
        cmd, _ = clip(str(inp.get("command", "")), MAX_INPUT)
        out["command"] = cmd
        if inp.get("description"):
            out["description"] = redact(str(inp["description"]))[:300]
        out["summary"] = cmd.splitlines()[0][:200] if cmd else "bash"
    elif name in ("Read", "NotebookRead"):
        rng = ""
        if inp.get("offset") or inp.get("limit"):
            rng = f" (lines {inp.get('offset') or 1}–{(inp.get('offset') or 1) + (inp.get('limit') or 0)})"
        out["summary"] = (out.get("path") or "file") + rng
    elif name == "Edit":
        d = unified_diff(fp, str(inp.get("old_string", "")), str(inp.get("new_string", "")))
        out["diff"], out["diff_truncated"] = clip(d, MAX_DIFF)
        out["summary"] = out.get("path") or "file"
        if inp.get("replace_all"):
            out["summary"] += " (replace all)"
    elif name == "MultiEdit":
        diffs = [unified_diff(fp, str(e.get("old_string", "")), str(e.get("new_string", "")))
                 for e in inp.get("edits") or [] if isinstance(e, dict)]
        out["diff"], out["diff_truncated"] = clip("\n".join(diffs), MAX_DIFF)
        out["summary"] = f"{out.get('path') or 'file'} ({len(diffs)} edits)"
    elif name == "Write":
        content = str(inp.get("content", ""))
        out["diff"], out["diff_truncated"] = clip(unified_diff(fp, "", content), MAX_DIFF)
        out["summary"] = f"{out.get('path') or 'file'} ({len(content.splitlines())} lines)"
    elif name in ("Grep", "Glob"):
        pat = str(inp.get("pattern", ""))
        out["summary"] = redact(pat)[:200] + (f" in {_short_path(str(inp['path']))}" if inp.get("path") else "")
    elif name == "StructuredOutput":
        out["summary"] = str(inp.get("outcome") or inp.get("verdict") or "structured output")
        txt, _ = clip(json.dumps(inp, indent=2, ensure_ascii=False), MAX_INPUT)
        out["input"] = txt
    elif name in ("Task", "Agent"):
        out["summary"] = redact(str(inp.get("description") or inp.get("subagent_type") or name))[:200]
        txt, _ = clip(str(inp.get("prompt", "")), MAX_INPUT)
        out["input"] = txt
    elif name in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        out["summary"] = name
        txt, _ = clip(json.dumps(inp, ensure_ascii=False), MAX_INPUT)
        out["input"] = txt
    else:
        txt, _ = clip(json.dumps(inp, ensure_ascii=False), MAX_INPUT)
        out["input"] = txt
        out["summary"] = txt.splitlines()[0][:200] if txt else name
    return out


def _usage(u: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(u, dict):
        return None
    out = {k: int(u.get(src) or 0) for k, src in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                                                 ("cache_read_tokens", "cache_read_input_tokens"),
                                                 ("cache_write_tokens", "cache_creation_input_tokens"))}
    return out


def feed_from_cli_events(events: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Claude Code stream-json events (cli_events) → feed items. Tool results are attached to their call."""
    items: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}
    seen_msg_usage: set[str] = set()
    meta: dict[str, Any] = {}
    last_ts: str | None = None
    for ev in events:
        typ, sub = ev.get("type"), ev.get("subtype")
        ts = ev.get("timestamp") or last_ts
        last_ts = ts
        if typ == "system" and sub == "init":
            meta.update({k: ev.get(k) for k in ("model", "claude_code_version", "permissionMode", "session_id")
                         if ev.get(k) is not None})
            meta["tools"] = [t for t in ev.get("tools") or [] if isinstance(t, str)][:50]
            items.append({"kind": "session_start", "at": ts, "model": ev.get("model"),
                          "cli_version": ev.get("claude_code_version"),
                          "summary": f"session started · {ev.get('model', '?')} · claude-code "
                                     f"{ev.get('claude_code_version', '?')}"})
        elif typ == "system" and sub == "api_retry":
            items.append({"kind": "api_retry", "at": ts, "summary": "model API retry",
                          **({"attempt": ev["attempt"]} if isinstance(ev.get("attempt"), int) else {})})
        elif typ == "assistant":
            msg = ev.get("message") or {}
            mid = str(msg.get("id") or "")
            usage = None
            if mid and mid not in seen_msg_usage:
                seen_msg_usage.add(mid)
                usage = _usage(msg.get("usage"))
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                base: dict[str, Any] = {"at": ts, "model": msg.get("model")}
                if usage is not None:
                    base["usage"] = usage
                    usage = None
                if ct == "text":
                    txt, tr = clip(str(c.get("text", "")), MAX_TEXT)
                    items.append({"kind": "assistant_text", **base, "text": txt, **({"truncated": True} if tr else {})})
                elif ct == "thinking":
                    items.append({"kind": "thinking", **base, "length": len(str(c.get("thinking", ""))),
                                  "summary": "thinking (content not shown)"})
                elif ct == "tool_use":
                    name = str(c.get("name", "tool"))
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    it = {"kind": "tool_call", **base, "tool": name, "tool_use_id": c.get("id"),
                          **describe_tool(name, inp)}
                    if ev.get("parent_tool_use_id"):
                        it["parent_tool_use_id"] = ev["parent_tool_use_id"]
                    items.append(it)
                    if c.get("id"):
                        calls[str(c["id"])] = it
        elif typ == "user":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                txt, _ = clip(content, MAX_TEXT)
                items.append({"kind": "user_message", "at": ts, "text": txt})
                continue
            tur = ev.get("tool_use_result") if isinstance(ev.get("tool_use_result"), dict) else {}
            for c in content or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    tid = str(c.get("tool_use_id", ""))
                    raw = _result_text(c.get("content"))
                    if tur.get("stderr"):
                        raw = (raw + "\n[stderr]\n" + str(tur["stderr"])) if raw else str(tur["stderr"])
                    txt, tr = clip(raw, MAX_RESULT)
                    res = {"at": ts, "is_error": bool(c.get("is_error")), "text": txt, "truncated": tr,
                           "lines": raw.count("\n") + 1 if raw else 0}
                    if tur.get("interrupted"):
                        res["interrupted"] = True
                    call = calls.get(tid)
                    if call is not None:
                        call["result"] = res
                    else:
                        items.append({"kind": "tool_result", "tool_use_id": tid, **res})
                elif c.get("type") == "text":
                    txt, _ = clip(str(c.get("text", "")), MAX_TEXT)
                    items.append({"kind": "user_message", "at": ts, "text": txt})
        elif typ == "result":
            u = _usage(ev.get("usage")) or {}
            it = {"kind": "result", "at": ts, "subtype": sub, "is_error": bool(ev.get("is_error")),
                  "num_turns": ev.get("num_turns"), "duration_ms": ev.get("duration_ms"), "usage": u,
                  "summary": f"result: {sub}"}
            if isinstance(ev.get("total_cost_usd"), (int, float)):
                it["cost_usd"] = round(float(ev["total_cost_usd"]), 6)
            if ev.get("terminal_reason"):
                it["terminal_reason"] = ev["terminal_reason"]
            items.append(it)
            meta["result"] = {k: it.get(k) for k in ("subtype", "num_turns", "cost_usd", "is_error")}
    nxt = None  # events before the first timestamped one (system/init) take the next known time
    for it in reversed(items):
        if it.get("at"):
            nxt = it["at"]
        elif nxt:
            it["at"] = nxt
    return items, meta


def feed_from_transcript(records: Iterable[dict[str, Any]], *, include_agent: bool) -> list[dict[str, Any]]:
    """replicant.transcript/v1 records. With cli_events present only the records cli_events lacks are used
    (the first user message and steering); otherwise (`include_agent`) the whole feed comes from here."""
    items: list[dict[str, Any]] = []
    for r in records:
        kind = r.get("kind")
        ts = r.get("ts")
        if kind in ("user_message", "steering", "resume_message", "continue"):
            txt, tr = clip(str(r.get("input_preview", "")), MAX_TEXT)
            items.append({"kind": "steering" if kind == "steering" else "user_message", "at": ts, "text": txt,
                          "untrusted": True, **({"truncated": True} if tr else {})})
        elif not include_agent:
            continue
        elif kind == "assistant_text":
            txt, tr = clip(str(r.get("input_preview", "")), MAX_TEXT)
            items.append({"kind": "assistant_text", "at": ts, "text": txt, "model": r.get("model")})
        elif kind == "tool_use":
            name = str(r.get("tool", "tool"))
            try:
                inp = json.loads(r.get("input_preview") or "{}")
            except ValueError:
                inp = {"input": r.get("input_preview")}
            items.append({"kind": "tool_call", "at": ts, "tool": name, "tool_use_id": r.get("tool_use_id"),
                          **describe_tool(name, inp if isinstance(inp, dict) else {})})
        elif kind == "tool_result":
            for it in reversed(items):
                if it.get("tool_use_id") == r.get("tool_use_id") and it["kind"] == "tool_call":
                    it["result"] = {"at": ts, "is_error": bool(r.get("is_error")), "text": "", "lines": 0}
                    break
        elif kind == "session_start":
            items.append({"kind": "session_start", "at": ts, "model": r.get("model"),
                          "summary": f"session started · {r.get('model', '?')}"})
        elif kind == "thinking_summary":
            items.append({"kind": "thinking", "at": ts, "length": r.get("length"),
                          "summary": "thinking (content not shown)"})
        elif kind == "result":
            items.append({"kind": "result", "at": ts, "subtype": r.get("subtype"), "num_turns": r.get("num_turns"),
                          "summary": f"result: {r.get('subtype')}"})
        elif kind == "api_retry":
            items.append({"kind": "api_retry", "at": ts, "summary": "model API retry"})
    return items


def log_items(body_parts: Iterable[bytes]) -> list[dict[str, Any]]:
    """sandbox_log chunks (QA harness and bootstrap output, CLI stderr) → one `log` item per line (tail)."""
    lines: list[str] = []
    for b in body_parts:
        lines.extend(b.decode("utf-8", errors="replace").splitlines())
    dropped = max(0, len(lines) - MAX_LOG_LINES)
    out = [{"kind": "log", "text": redact(l[:2000])} for l in lines[-MAX_LOG_LINES:] if l.strip()]
    if dropped:
        out.insert(0, {"kind": "log", "text": f"…[{dropped} earlier log lines not shown]"})
    return out


def build_feed(cli_events: list[dict[str, Any]], transcript: list[dict[str, Any]],
               logs: list[bytes]) -> dict[str, Any]:
    if cli_events:
        items, meta = feed_from_cli_events(cli_events)
        # the opening prompt and steering come only from the transcript (stdin is not echoed on stdout)
        extra = feed_from_transcript(transcript, include_agent=False)
        items = sorted(extra + items, key=lambda i: (i.get("at") or ""))  # stable: equal stamps keep order
    else:
        items, meta = feed_from_transcript(transcript, include_agent=True), {}
    log = log_items(logs)
    tu: dict[str, int] = {}
    for it in items:
        for k, v in (it.get("usage") or {}).items() if it["kind"] != "result" else ():
            tu[k] = tu.get(k, 0) + int(v)
    if tu:
        meta["transcript_usage"] = tu  # per assistant message, as the CLI saw it (the gateway log lags while live)
    counts: dict[str, int] = {}
    tools: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
        if it["kind"] == "tool_call":
            tools[it["tool"]] = tools.get(it["tool"], 0) + 1
    dropped = max(0, len(items) - MAX_ITEMS)
    items = items[-MAX_ITEMS:]
    for i, it in enumerate(items):
        it["i"] = dropped + i
    return {"items": items, "log": log, "meta": meta, "counts": counts, "tools": tools, "items_dropped": dropped}


def usage_rows(events: Iterable[dict[str, Any]], run_id: str) -> dict[str, Any]:
    """replicant.gateway-usage/v1 events of one run → per-request rows and totals. The caller block (token and
    grant JTIs) is never surfaced."""
    rows = []
    for e in events:
        if e.get("run_id") != run_id:
            continue
        u = e.get("usage") or {}
        c = e.get("cost") or {}
        rows.append({"at": e.get("occurred_at"), "model": e.get("served_model") or e.get("gateway_model_id"),
                     "status": e.get("status"), "http_status": e.get("http_status"),
                     "input_tokens": int(u.get("input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0),
                     "cache_read_tokens": int(u.get("cache_read_tokens") or 0),
                     "cache_write_tokens": int(u.get("cache_write_tokens") or 0),
                     "cost_usd": round(int(c.get("micros_usd") or 0) / 1e6, 6), "latency_ms": e.get("latency_ms"),
                     "retry_attempt": e.get("retry_attempt", 0), "purpose": e.get("purpose"),
                     "trace_id": e.get("trace_id"),
                     **({"run_spent_usd": round(int((e.get("budget_after") or {}).get("run_spent_micros") or 0) / 1e6, 6)}
                        if e.get("budget_after") else {})})
    rows.sort(key=lambda r: r["at"] or "")
    tot = {k: sum(r[k] for r in rows) for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                                 "cache_write_tokens")}
    tot["cost_usd"] = round(sum(r["cost_usd"] for r in rows), 6)
    tot["requests"] = len(rows)
    tot["errors"] = sum(1 for r in rows if r["status"] not in ("ok", None))
    return {"requests": rows, "totals": tot, "models": sorted({r["model"] for r in rows if r["model"]})}
