# ADR-08-006: Langfuse generations come from the gateway shim's spans; content comes from the uploader

- **Status:** Accepted (WS-08, 2026-10-24)
- **Constraints:** D-11 (gateway routing), D-17, D-21

## Context

Three sources could produce one Langfuse generation per model call:

1. **replicant-gateway-shim** (WS-03 gateway.md section 7): span `gateway.request`, child of the caller's
   `traceparent`, with `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
   `replicant.cost_micros`, and the Replicant identity attributes. It sees every model call: Claude Code, Codex,
   and in-worker calls (router, triage, extraction). Its usage is the cost ledger's input (authoritative).
2. **Claude Code beta tracing**: `claude_code.llm_request` spans with model, token counts, `duration_ms`, `ttft_ms`
   [verified: https://code.claude.com/docs/en/monitoring-usage, 2026-10-24]. Claude only; beta.
3. **The entrypoint/uploader** parsing stream-json `assistant` messages or Codex `turn.completed.usage`
   (per turn, not per call) [verified: codex-rs/exec/src/exec_events.rs at rust-v0.164.0, 2026-10-24].

Langfuse maps `gen_ai.usage.*`, `gen_ai.request.model`, `langfuse.observation.*` attributes from OTLP spans
[verified: https://langfuse.com/integrations/native/opentelemetry, 2026-10-24].

## Decision

- **Generations: source 1 only.** The core Alloy gateway marks `gateway.request` spans
  `langfuse.observation.type=generation`, sets `langfuse.observation.model.name` and
  `langfuse.observation.cost_details={"total": cost_micros / 1e6}`. Generation spans carry **no prompt or
  completion content** (the shim never exports bodies).
- **Claude `claude_code.llm_request` spans** are kept as plain spans (timing, TTFT) in Tempo and Langfuse; never
  generations, so usage is not double counted. Other `claude_code.*` spans go to Tempo only.
- **Content** (redacted, truncated) comes from the sandbox uploader's transcript processing: `tool`
  observations (tool name, input preview at most 2 KiB, output preview at most 4 KiB) and `event` observations
  for assistant text (at most 4 KiB), emitted as OTLP spans with instrumentation scope `replicant.agent`
  (session-capture.md section 6).

## Consequences

- One generation per real model call, with authoritative cost, for every CLI and for worker LLM calls.
- Langfuse cost is the ledger's cost, not Langfuse's model price table.
- If the shim is bypassed (it is not, by D-11 and network policy), generations are missing; the alert
  `ReplicantTelemetryProbeFailing{sink="langfuse"}` covers pipeline loss, not bypass.

## Revisit when

- The CLIs export per-call spans with content controls that make the uploader-derived observations redundant.
