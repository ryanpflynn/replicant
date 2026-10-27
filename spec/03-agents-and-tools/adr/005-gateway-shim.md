# ADR-03-005: Replicant-owned gateway shim in front of the enterprise AI gateway

- **Status:** Proposed pending OQ-00-09 / OQ-03-01 (WS-03, 2026-10-24); Replicant is designed against it.
- **Constraints:** D-11, D-17 (open source, self-hosted), D-21, D-23

## Context

Replicant needs per-run credentials, per-run/job/tenant budgets, tenant model allowlists, attribution, and usage
export (gateway.md GW-1 to GW-12). The internal gateway's capabilities are unknown. Candidate shims (facts verified
2026-10-24): LiteLLM proxy 1.106.3 has OSS virtual keys and budgets, but JWT auth is Enterprise ("JWT Auth is an
enterprise only feature") and its budgets and keys live in its own Postgres [verified:
https://docs.litellm.ai/docs/proxy/token_auth, 2026-10-24]; custom auth is OSS but budgets and model checks require
extra flags, and custom auth combined with virtual keys is Enterprise [verified:
https://docs.litellm.ai/docs/proxy/custom_auth, 2026-10-24]. Agent Router (formerly Envoy AI Gateway) v1.2.0 does
usage-based rate limiting through Envoy's global rate limit service, charging after the response with limits configured
per descriptor [verified: https://github.com/theagentrouter/agent-router, 2026-10-24]. Kong's token/cost rate limiting is
Enterprise-only [verified: https://developer.konghq.com/plugins/ai-rate-limiting-advanced/, 2026-10-24].

## Options

1. **Use the enterprise gateway directly** (if it supports everything). Unknown.
2. **LiteLLM proxy with per-run virtual keys** minted by Replicant at run start: a second credential type, key
   lifecycle in LiteLLM's database, JWT verification requiring an Enterprise license.
3. **Agent Router**: strong data plane, but per-run dynamic budgets taken from a JWT claim do not fit static
   rate-limit descriptors.
4. **Thin replicant-owned Go shim (chosen)**: verifies the existing run token, enforces budgets in Valkey, emits usage
   events, forwards to the enterprise gateway unchanged.

## Decision

Option 4: `replicant-gateway-shim` (gateway.md §4). It is small (pass-through proxy, JWT verification, Lua budget
scripts, SSE usage parsing) and reuses the Runner's run token, so no second credential exists. If the enterprise
gateway later implements GW-1 to GW-12, the shim is removed by configuration.

## Consequences

- Replicant owns a hot-path service (HA, on-call). Mitigated by statelessness and a documented break-glass.
- Protocol pass-through means no protocol translation: each CLI must talk a protocol the enterprise gateway offers.

## Revisit when

- The enterprise gateway owner confirms GW-1 to GW-12 natively, or an OSS gateway supports per-request budgets from
  JWT claims without an Enterprise license.
