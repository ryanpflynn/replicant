# ADR-07-001: Server-Sent Events for live updates

- **Status:** Accepted (WS-07, 2026-10-24) · resolves the C-STATE open question "SSE or WebSocket"
- **Constraints:** §2 SLO (first visible progress < 60 s), D-09 (UI reads Postgres), ADR-004

## Context

The UI needs push for job progress, inbox changes, and action outcomes. The projector already emits
`NOTIFY replicant_job_updates '<tenant>:<job_id>:<seq>'` (C-STATE §2.1). Traffic is server → client only;
human actions are ordinary authenticated POSTs. About 600 concurrent viewers at peak (2,000 engineers).

## Options

1. **SSE** (`text/event-stream`, `sse-starlette` 3.5.1 on FastAPI). One-way, plain HTTP, automatic
   reconnect with `Last-Event-ID`, passes ALBs and corporate proxies, works with cookie auth and CSRF-free
   GETs. Over HTTP/2 it does not consume the browser's per-host connection limit.
2. **WebSocket.** Bidirectional (unneeded), separate auth handshake, harder through proxies, needs its own
   heartbeat and reconnect logic, and invites "actions over the socket", which would bypass the per-request
   authorization and outbox path.
3. **Polling** every 5 s. Simple, but 600 viewers × list queries every 5 s is wasteful and still slower
   than push.

## Decision

SSE, with **reference-only events** (`job.updated {job_id, seq}`, `inbox.changed`, `action.completed`,
`release.changed`, `resync`). The client re-fetches through the normal authorized GET endpoints. Each API
replica holds one `LISTEN` connection and fans out in process; topics are authorized at subscribe time and
every 5 minutes; a 5-minute ring buffer serves `Last-Event-ID` replays; 15-second keep-alive comments.

## Consequences

- No data crosses the stream, so a missed authorization change can at most leak that an already-visible job
  changed; data access is always re-authorized.
- One extra GET per event per viewer; bounded by coalescing (the client debounces 250 ms per key).
- Live transcript tails (Loki) reuse the same SSE mechanism on a separate endpoint.

## Revisit when

- Concurrent viewers exceed ~5,000 or collaborative features need client → server streaming.
