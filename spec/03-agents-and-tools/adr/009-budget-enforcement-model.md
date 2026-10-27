# ADR-03-009: Budget enforcement: reservation admission, bounded overshoot, 402 on exhaustion

- **Status:** Accepted (WS-03, 2026-10-24)
- **Constraints:** D-11, D-03 (budgets are behavioral, in the bundle), ADR-013 (quotas are operational)

## Context

Cost is known only after a response (output tokens). Streams cannot be cut off mid-response without corrupting the
agent's turn. Runs of one job can execute in parallel (QA and reviewer). CLIs retry 429 automatically (Claude Code up to
`CLAUDE_CODE_MAX_RETRIES`, default 10 [verified: https://code.claude.com/docs/en/env-vars.md, 2026-10-24]), so a budget
error delivered as 429 would loop. LiteLLM's default for budget exhaustion is 422 [verified:
https://docs.litellm.ai/docs/proxy/team_budgets, 2026-10-24]; Anthropic uses 402 `billing_error` for billing
[verified: https://platform.claude.com/docs/en/api/errors, 2026-10-24].

## Options

1. **Post-hoc only** (charge after; deny next request once over): simple; overshoot unbounded with parallel requests.
2. **Strict estimate admission** (deny if spent + estimate > limit): no overshoot, but conservative estimates refuse
   legitimate final requests well under budget.
3. **Reservation admission (chosen):** deny when `spent + reserved ≥ limit`; reserve an estimate per in-flight
   request; settle actual cost; cap in-flight requests per run so overshoot ≤ max_concurrency × max request cost.

## Decision

Option 3 with run, job, and tenant-period levels in one atomic Valkey Lua script (gateway.md §6.2). Budget exhaustion
is HTTP 402 `budget_exceeded` with `X-Replicant-Budget-Exceeded`; agentd converts it to a non-retryable protocol error
for the CLI and the entrypoint exits 20 (`GatewayBudgetExceeded`). Per-run budgets are computed by the workflow from the
job budget (allocation), so the job counter at the gateway is a backstop. Tenant period quotas are operational
configuration read live; job and run budgets come from the bundle's `budgets` policy.

## Consequences

- Overshoot is bounded and reported (typically under $2 per run at default models and concurrency 4).
- The gateway state store is on the hot path; failure is fail-closed after 60 s.

## Revisit when

- Providers support server-side per-request spend caps that the gateway can pass through.
