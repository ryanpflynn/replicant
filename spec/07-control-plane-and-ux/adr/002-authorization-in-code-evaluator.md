# ADR-07-002: Authorization as one in-code evaluator (`replicant-authz`)

- **Status:** Accepted (WS-07, 2026-10-24); WS-09 concurs (control-review SC-55)
- **Constraints:** D-18 (groups + live repo permission, no confused deputy), D-25 (admin-only memory, no
  cached grants), D-23 (Python data access)

## Context

Every decision combines (a) Okta groups (SCIM replica or live Okta read), (b) role bindings per tenant,
(c) a live GitHub/Gitea repository permission, (d) action rules (self-approval, eligibility, delegation,
tenant match, D-25). Decisions are needed in control-plane-api (every request), identity-service (for
adapters and workflow activities), and memory-service (D-25 re-check).

## Options

1. **OPA** sidecar with Rego. Policy as data, mature; but the inputs that matter (live GitHub permission,
   live Okta groups) still have to be fetched by the caller, the policy surface is a fixed RBAC table plus
   five rules, and a sidecar adds a network hop and a second language.
2. **Cedar** (`cedarpy` 4.12.2, maintained). Analyzable policies and a good fit for RBAC/ABAC; same
   input-fetching issue; tenant admins do not author policies (they edit bindings), so analyzability buys
   little now.
3. **In-code evaluator** (chosen): one Python package, property-tested, used in process by
   control-plane-api and served over HTTP by identity-service; bindings are data (`identity.role_bindings`).

## Decision

Option 3. Inputs come from the SCIM replica (or live Okta for `freshness=live`), bindings, and live
repository permission; results carry evidence (groups and source, permission and source, `as_of`) for
audit. Exhaustive table tests and Hypothesis property tests (e.g. "no binding grants `memory:admin`",
"`gate:decide` never allows the requester when `requester_may_decide=false`").

## Consequences

- One implementation, no drift between enforcement points.
- Changing rules is a code change (reviewed, released), which is appropriate for security rules.

## Revisit when

- Tenants need to author their own authorization policies, or regulators require formal policy analysis:
  then Cedar, with the same input-fetching layer.
