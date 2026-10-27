# ADR-06-006: Evaluation mode resolves gates inside the workflow (no service actor decides gates)

- **Status:** Accepted (WS-06, 2026-10-24). Requires CCR-06-01.
- **Constraints:** D-15, D-18, D-19; C-MSG `gate.decide` validator V8 ("services never decide gates")

## Context

Eval and shadow jobs open the same gates as production jobs (plan, publish, clarification, budget). No human
can answer 1,800 gates for a T3 run, and V8 forbids services from deciding gates. PR-repair tasks also need
review comments, which D-15 only accepts from authorized humans.

## Options

1. **Synthetic human principals** (Okta users for robots) that decide through `gate.decide`. Exercises the full
   Update path, but creates fake humans in the identity system, weakens D-18, and needs SCIM exceptions.
2. **Exempt a service actor from V8 for tenant `replicant-eval`.** A contract change to the most security-sensitive
   validator, which risks accidental generalization.
3. **Evaluation mode (chosen):** a JobRequest option accepted only from principals with `release:pin` and
   `eval:run`. The workflow resolves each gate from a script by calling the same internal transition the
   Update handler uses, emits `gate.auto_resolved`, and replaces `pr.event` waits with scripted review rounds.

## Decision

Option 3 ([schemas/evaluation-options.schema.json](../schemas/evaluation-options.schema.json)). V8 and D-15 stay
untouched for every non-evaluation job. Evaluation mode also suppresses notifications and publishing.

## Consequences

- Gate and repair **logic** (risk rules, which gates open, repair cycles) is evaluated. Gate **adapters**
  (Slack, GitHub, identity checks) are not, and are covered by integration tests and online metrics.
- WS-01 must guarantee that the evaluation-mode resolver and the Update handler share one transition function
  (XD-06-02), so the two paths cannot diverge.
- control-plane-api must reject `options.evaluation` from any other principal (403), and publisher-worker
  must refuse publishes for jobs in evaluation mode (defense in depth).

## Revisit when

- A staging-only "full fidelity" eval with real PR flows on the eval Git server is built (OQ-06-07).
