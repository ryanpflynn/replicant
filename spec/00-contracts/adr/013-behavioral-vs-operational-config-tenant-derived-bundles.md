# ADR-013: Behavioral vs operational configuration; tenant-derived bundles

- **Status:** Accepted (WS-00, 2026-10-24). **Amended by CCR-06-06:** the tightening check follows
  WS-06 [ADR-06-001](../../06-releases-and-evals/adr/001-tightening-only-derived-bundles-auto-promote.md),
  which is normative.
- **Constraints:** D-03 (bundle includes policy versions and fully determines behavior), D-19
  (policy-driven gates), WS-07 (tenant admins manage their tenant's policies)

## Context

40 tenants each own policies: risk and gate rules, approval channels, budgets, and codebase
bindings. If every tenant policy edit required a new global bundle with a full eval, tenants would
be coupled to each other and the release pipeline would be congested. If tenant policy were read
live, outside the bundle, D-03 would be violated and jobs would no longer be reproducible.

## Options

1. **All tenant policy inside the one global bundle.** Coupled, slow.
2. **Tenant policy outside bundles, read live.** Violates D-03.
3. **Tenant-derived bundles (chosen).** `derived(base, tenant_layer)` is itself a full,
   content-addressed bundle.

## Decision

- **Behavioral configuration** (what agents do and which gates exist) is always in a bundle:
  graphs, agents, environments, prompts, CLI images, models, risk, gate, budget, runtime and
  network, memory snapshot, codebase bindings, and repair limits.
- **Operational configuration** (where to deliver, how much capacity, emergency stops) is read live
  and recorded when used: Slack channel IDs and reviewer team targets, notification preferences,
  quotas, bundle assignment rules, identity data, kill switches, and memory entry states (memory
  content enters a job only through its pinned snapshot). Operational configuration may change
  **where**, **when**, and **whether**. It never changes **how** an agent behaves.
- A tenant's layer is the set of tenant-scoped `Policy` versions. Tenant-layer-eligible policy types:
  `gate_rules`, `risk_rules`, `approval_channels`, `budgets`, `repair_limits`, `network_profiles`,
  `retention`, `codebase_bindings`, `runtime_selection`, `memory_snapshot`. `router_rules` and
  `runner_polling` are **global only**. Whenever the base bundle or the
  tenant layer changes, the registry builds `derived(base, layer)` with `spec.tenant_layer` set.
  Assignment for tenant T resolves base B to `derived(B, layer_T)`.
- Promotion of a derived bundle follows the eval tier **T0** (the policy-differential check) of
  ADR-06-001. It auto-promotes, performed by `svc_release-controller`, when:
  - every element of the derived spec other than `policies` and `tenant_layer` is digest-equal to B;
  - every tenant policy classifies as `tightening` or `neutral` under the per-policy-type safety order
    in ADR-06-001;
  - budget sufficiency holds, or the tenant admin explicitly acknowledged it;
  - B is `promoted` or `canary`. The derived bundle follows B's state.

  For bindings specifically, **changing which environment or agent a repo uses is behavioral**, even
  when both definitions are in B's closure, because the (codebase type, definition) combination is
  unmeasured. Only graph modifiers that insert `check_only` node types, and no-op selectors, count as
  tightening or neutral. `runtime_selection` and `memory_snapshot` changes are always behavioral. A
  behavioral layer goes to a targeted T1 eval plus two-person sign-off (a platform release manager and
  a tenant admin who is not the layer's author).

## Consequences

- D-03 holds exactly: every job runs on one bundle that fully determines its behavior.
- Tenant admins change their policies without waiting for a global release.
- There are many bundles (base × tenants). The UI groups them by base.

## Revisit when

- Tenant layers grow beyond policies (tenant-specific prompts), which would need real evals per
  tenant.
