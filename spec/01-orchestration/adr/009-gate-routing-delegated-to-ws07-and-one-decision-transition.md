# ADR-01-009: Gate presence and timing come from one WS-07 activity; every gate decision (human or evaluation script) goes through one transition function

- **Status:** Accepted (WS-01, 2026-10-24)
- **Constraints:** D-19 (deterministic gate presence; reminders, escalation, expiry), C-MSG `gate.decide`
  V1–V10, XD-07-01, XD-06-02, ADR-06-006 (evaluation mode)

## Context

WS-07 owns the `gate_rules` and `approval_channels` policy schemas (rule lists, ISO 8601 durations,
`when.any_of`, channel routes, eligibility). WS-01 owns when gates open in the graph, the Update handler,
and the timers. WS-06 needs evaluation and shadow jobs to resolve gates from a script without a human and
without weakening validator V8 ("services never decide gates").

## Options

1. WS-01 parses WS-07's policy in workflow code. This couples the workflow to WS-07's schema evolution,
   and every gate_rules schema change becomes a worker build.
2. **Activity `gates.resolve_routing` (WS-07 logic) returns a normalized
   [GateRouting](../schemas/gate-routing.v1.json) (required, channels, eligible, timing in seconds,
   on_expiry, reapprove_after_rebase) (chosen).**

## Decision

- The gate node calls `gates.resolve_routing` once per gate opening with deterministic facts (gate kind,
  workflow type, risk level and rules fired, requester, repos, changed-path summary, requested amount for
  budget gates). The result is memoized under the node's logical ID (`<node>.c<c>.n<o>.route`) and
  recorded in history, so replay never re-evaluates the policy. Gate presence stays deterministic (D-19):
  same pinned bundle plus same facts gives the same result. Graph `when:` and
  `JobRequest.constraints.require_gates` can only add gates.
- **One decision transition.** `ReplicantJobBase._apply_decision(gate, decision, source)` is the only code
  that closes a gate. It is called by:
  - the `gate.decide` Update handler, after validators V1–V10 and the authoritative
    `identity.check_decision` activity (C-MSG); and
  - the evaluation-mode resolver, after activity `eval.read_gate_script` (WS-06 eval-harness.md §3.3; CCR-06-01, C-MSG 1.3.0). This path
    has no Update, no human principal, and no identity check. The timeline event is `gate.auto_resolved`
    with the script rule index; the decision's `principal_id` is the job's requester (the principal that
    submitted the scripted case) and `channel` is `system`. No `gate.decide` Update is sent, so no service
    principal decides a gate through the Update path (C-MSG 1.3.0 §2.1).
  Both paths produce the same gate state, approval record, projection, and wake-up.
- Validator V8 is unchanged. Evaluation mode cannot be reached by an Update: `options.evaluation` is part
  of the start input, accepted by control-plane-api only from principals with `release:pin` and `eval:run`.

## Consequences

- WS-07 can evolve its policy schema without worker builds, as long as `GateRouting` v1 stays stable (a
  change to it is CC-4).
- Evaluation mode exercises gate presence logic, packets, timers, and the transition. It does not exercise
  channel adapters or the identity check, which WS-07 and WS-11 integration tests cover (ADR-06-006).

## Revisit when

- WS-07 needs gate decisions that depend on facts only available at decision time (they would move into
  `identity.check_decision`, which already runs at decision time).
