# ADR-06-001: Tightening-only tenant-derived bundles auto-promote (decides OQ-00-05)

- **Status:** Accepted (WS-06, 2026-10-24). Decides OQ-00-05, which the orchestrator deferred to WS-06.
- **Constraints:** D-03 ("Bundles are promoted only after passing the evaluation harness"), D-19, D-25,
  ADR-013, C-DEF §4.4
- **Related:** [release-bundle.md §9](../release-bundle.md#9-tenant-layers-and-derived-bundles),
  [promotion.md §4.2](../promotion.md#42-t0-tenant-derived), CCR-06-06

## Context

ADR-013 makes every tenant policy change produce a full, content-addressed bundle `derived(B, L_T)`, so that
D-03's "fully determines behavior" holds for all 40 tenants. It proposes that a derived bundle auto-promotes
when its layer passes a mechanical *tightening check*, and leaves the eval tier to WS-06. The orchestrator asked
WS-06 to decide whether that auto-promotion is consistent with D-03's requirement that bundles are promoted
"only after passing the evaluation harness".

The numbers that make the question non-academic:

* 40 tenants. Each base release (3–5 per week, promotion.md §12) produces up to 40 derived bundles, and each
  tenant policy edit produces 1–2 (current default and ramp candidate).
* A T2 evaluation costs about $2.2k at the modelled token profile and $3.8k at 2× (eval-harness.md §8). Running
  T2 on every derived bundle of one base release would cost 40 × $2.2k ≈ $88k. At 3–5 base releases per week
  that is about $1.1M–$1.9M per month at the modelled token profile, before counting tenant policy edits, and
  it would need about 40 × 3.4 h of eval capacity per base release.
* In the harness, gates are resolved by script (evaluation mode, eval-harness.md §3.3). A tenant's gate rules,
  risk rules and approval channels therefore **cannot change any measured outcome**: an eval of a derived
  bundle whose layer only adds gates would reproduce the base's results exactly, except for sampling noise.

## What D-03 protects

D-03's evaluation requirement exists so that no job runs on agent behavior that has not been measured. In a
derived bundle, every element that determines agent behavior is copied from B by digest: graphs, router, worker
version and NodeCatalog, agent definitions, prompts, CLI images, models, environments, and memory snapshot
policy. Only `policies` and `tenant_layer` differ. The question is therefore whether a policy change can change
measured agent behavior in an unmeasured direction.

## Options

1. **Full eval tier for every derived bundle.** Meets the letter of D-03 at a cost of six to seven figures per
   month, and measures nothing the base eval did not, because the harness scripts gates. It would also couple
   tenants to the release pipeline, which ADR-013 exists to avoid.
2. **Smoke eval (T1) for every derived bundle.** About $219 × 40 per base release. Still measures nothing for
   gate-only layers. The 60-task smoke set cannot detect budget-induced regressions smaller than about 16 points.
3. **Mechanical policy-differential verification as harness tier T0, with eval tiers for anything that is not
   provably tightening or neutral (chosen).**

## Decision

1. The evaluation harness has a tier **T0**, the policy-differential check. It is part of the harness: run by
   the release controller under the promotion policy, recorded in `registry.bundle_promotion_events` with
   `tier = T0` and the full tightening report as evidence. A derived bundle that passes T0 has "passed the
   evaluation harness" in the sense of D-03.
2. **T0 passes** when all of the following hold:
   * **G-T0-1 closure identity:** every element of `derived.spec` other than `policies` and `tenant_layer` is
     digest-equal to `B.spec`. This is checked structurally, not trusted.
   * **G-T0-2 classification:** every tenant policy in the layer is `tightening` or `neutral` under the order
     below.
   * **G-T0-3 budget sufficiency:** the tenant's caps are not below p75 of B's measured eval cost for the
     affected role or workflow type. If they are, the tenant admin must acknowledge (an explicit human
     decision). No eval is needed, because the effect of a lower cap is a budget gate or a fail-closed run.
   * **G-T0-4 base state:** B is `promoted` (the derived bundle becomes `promoted`) or `canary` (the derived
     bundle becomes `canary` and follows B).
3. **Anything else** (a `behavioral` classification) goes to a **targeted T1** eval: golden tasks matching the
   tenant's codebase types, at least 40, candidate only, compared with the results for `derived(B_prev, L_T)`.
   After it passes, promotion needs two signers at sign-off point `tenant_layer_behavioral` (CCR-06-11): one
   platform release manager and one tenant admin of that tenant, holding the tenant-scoped permission
   `release:signoff_tenant_layer`, other than the layer's author (two-person rule, WS-09 G-09). A behavioral layer can weaken gates, so it never
   promotes on a single person's action.
4. Auto-promotion is performed by `svc_release-controller` under the admin-configured promotion policy. It is
   never performed by tenant admins, who can change their layer but cannot change promotion state.

### Safety order per policy type (normative; owners implement the comparators)

"≤" means "at least as safe as the base, for every input". A comparator returns `tightening`, `neutral`, or
`behavioral` with the offending JSON paths. Unknown fields are `behavioral`.

| Policy type | Tightening (≤) | Neutral | Behavioral (anything else, including) | Comparator owner |
|---|---|---|---|---|
| `gate_rules` | For every (workflow type, risk level, path class) the tenant gate set ⊇ the base gate set; `requester_may_decide` true → false; minimum approvers ≥ base; eligible approver groups ⊆ base groups (narrower) | reminder and escalation intervals within platform bounds; expiry within [1 d, 30 d] | removing a gate; lowering approver counts; widening eligibility | WS-07 |
| `risk_rules` | Only additional rules whose effect is `raise_to: <level>` (D-19: rules may raise, never lower) | — | any change to base rules; any rule with a lowering effect | WS-07 with WS-09 |
| `approval_channels` | channels ⊆ base allowed channels | Slack channel targets (these are operational settings, not policy) | adding a channel | WS-07 |
| `budgets` | every cap ≤ base cap (per run, per job, per day); `budget_extension` gate required if the base requires it | — | raising any cap; removing the extension gate | WS-03 |
| `repair_limits` | max cycles ≤ base; max ordinals ≤ base | — | raising limits | WS-01 |
| `network_profiles` | tenant profile's FQDN allowlist ⊆ base profile's allowlist | — | any added destination | WS-02 with WS-09 |
| `retention` | — | values within the platform's [min, max] per artifact type (WS-10 bounds) | outside bounds | WS-10 |
| `codebase_bindings` | graph modifiers that only insert node types whose NodeCatalog `effectClass` is `check_only` (the node can fail the job or raise risk, cannot write the workspace or call external side effects), for example `license_scan` | adding repo selectors that resolve to exactly what the base binding would choose (no-op) | changing which environment or agent a repo uses; any other node insertion | WS-03 (needs `effectClass` from WS-01, XD-06-01) |
| `runtime_selection` | — | — | always behavioral: different runtimes change build and QA behavior (D-06) | WS-02 |
| `memory_snapshot` | — | — | **not tenant-layer-eligible** (global only): it decides which memory is injected, and D-25 reserves memory decisions to the Replicant admin group (R1-145) | WS-04 |

### Why tightening cannot produce an unmeasured regression

* **More gates or higher risk** change *whether a human is asked*, never what the agent produces. Human waits
  are bounded by D-19's reminders, escalation and expiry. The harness cannot measure gate presence (it scripts
  gates), but the online gate metrics per tenant (online-metrics.md §2) do.
* **Lower budgets** can stop work earlier. The effect is fail-closed: a `budget_extension` gate or
  `GatewayBudgetExceeded`, never different code. G-T0-3 makes a material effect an explicit tenant decision.
* **Fewer channels, narrower egress, lower repair limits** can only stop or slow work, never change its content.
* **Check-only node insertions** can only fail a job or raise its risk. Their own code is in B's NodeCatalog,
  already evaluated as part of B's worker version (CC-1 and CC-2).

What T0 does **not** cover, and where it is covered instead: the effect of extra gates on human wait time and
of lower budgets on success rate. These are tenant-chosen tradeoffs and are visible on the per-tenant online
dashboards. A tenant whose success rate drops by more than 10 points in the 7 days after a layer change gets an
automatic notice to its admins (WS-07 notification), not a rollback, because the layer is the tenant's decision.

## Consequences

- D-03 holds: every job runs on one bundle that fully determines its behavior, and every bundle reaching a
  non-eval tenant has passed a harness tier recorded with evidence.
- Tenant admins can tighten their policies with near-instant effect (one synchronous build plus T0, seconds).
- ADR-013's clause "bindings reference only definitions already in B's closure" is **narrowed**: a binding
  that moves a repo to a different environment or agent is behavioral even if both are in B's closure, because
  the moved repos run a combination that was never measured on their codebase type. CCR-06-06 asks for
  ADR-013's text to be updated to match.
- WS-01 must add `effectClass` to NodeCatalog node entries (XD-06-01), which the envelope schema already
  permits (node items allow additional properties).
- Each comparator is code with property-based tests (Hypothesis): for random base and tenant documents,
  `classify(base, tenant) == tightening` implies that the effective merged policy never permits an action the
  base forbids.

## Revisit when

- Tenant layers grow beyond policies (tenant prompts or tenant agent definitions). Those are behavioral and need
  real evals per tenant (ADR-013 "Revisit when").
- The harness starts measuring gate interaction (for example simulated approvers with latency). Then gate
  changes become measurable, and T0 for gate rules can be complemented by T1.
