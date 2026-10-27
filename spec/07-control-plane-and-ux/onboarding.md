# Tenant onboarding checklist

**Owner:** WS-07 (WS-09 reviews) · **Constraints:** D-21, D-18, D-24 · **Findings:** R2-001 (critical),
R2-055 · **Stored in:** `tenancy.tenants.onboarding_checklist` (JSON: item → `{attested_by, attested_at,
evidence_url}`), shown at Tenant settings → General and enforced where noted.

## Purpose

Replicant pushes agent-written code to `replicant/<job_id>` (publish and repair) and
`replicant-takeover/<job_id>` (takeover). Pushes made with a GitHub App installation token **do** trigger
`push` and `pull_request` workflows in the target repository (R2-001, re-checked by R2 on 2026-10-24), so
target CI can execute agent-written code with repository secrets before any human reviews the PR. The
Replicant's own controls (the non-removable `critical` class `ci_executed_paths`, the approved-digest
check in the Publisher, and the takeover prefix) cover what Replicant pushes; this checklist covers
what the tenant's CI does with it. A tenant is not set to `active` until the required items are attested.

## Checklist

| # | Item | Required | Enforced by the Replicant |
|---|---|---|---|
| OB-1 | **Secrets-bearing CI does not run for Replicant branches without approval.** Every workflow that can read deploy credentials, cloud OIDC roles, registry tokens, or other non-public secrets must not run automatically for `replicant/**` and `replicant-takeover/**`. Use one of the patterns below | yes | Takeover pushes that touch `ci_executed_paths` are refused until OB-1 is attested (github-app.md §10.1); the onboarding screen shows a read-only lint of `.github/workflows/*.yml` for each enabled repo (computed by WS-04's ecosystem-indexer, which holds `contents:read`; view `ecosystem.v_ci_workflow_lint`, XD-FIX-R2-001-WS07) and lists workflows that reference `secrets.*` or `id-token: write` without one of the patterns |
| OB-2 | Deploy credentials live in GitHub **Environments** with required reviewers, so a job only receives the environment's secrets after a human approves: "A job that references an environment must follow any protection rules for the environment before running or accessing the environment's secrets" [verified: https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment, 2026-10-24] | yes for repos with deploy workflows | lint reports repository-level deploy secrets used outside an environment |
| OB-3 | Branch rules on the default branch require at least one human approval, and "Require approval of the most recent reviewable push" is on, so every Replicant push needs a fresh human approval before merge | yes | read via `GET /repos/{o}/{r}/rules/branches/{branch}` at repo opt-in; warning in the UI when missing |
| OB-4 | `CODEOWNERS` exists for the paths risk rules care about (Replicant resolves gate approvers from it) | recommended | gate routing escalates immediately when CODEOWNERS resolves to no linked principal (approvals.md §2) |
| OB-5 | Tenant CI configuration paths beyond the base list are added to the tenant's `risk_rules.ci_executed_paths` layer (for example a repo-specific `tools/ci/**`) | recommended | tightening-only layer (ADR-013); WS-04's indexer also proposes entries parsed from workflow `run:` commands |
| OB-6 | Monthly and daily budgets set (defaults from `platform_caps` otherwise) | yes (schema-required) | `tenancy.tenant_settings` CHECK; intake refuses jobs over budget (R2-055) |
| OB-7 | Approval targets set (Slack approval and escalation channels) and tested with "Test delivery" | yes | gate routing fails over to tenant admins if unset |
| OB-8 | Okta groups for engineers, approvers, and leads exist and are bound (identity.md §5.3) | yes | — |

## OB-1 patterns (any one per secrets-bearing workflow)

1. **Branch filter on the trigger** (simplest):

   ```yaml
   on:
     push:
       branches-ignore: ["replicant/**", "replicant-takeover/**"]
     pull_request:
       # pull_request runs for the PR's head branch; skip replicant heads in the job condition below
   jobs:
     deploy-preview:
       if: ${{ !startsWith(github.head_ref, 'replicant/') && !startsWith(github.head_ref, 'replicant-takeover/') }}
       runs-on: ubuntu-latest
       environment: preview
       steps:
         - uses: actions/checkout@v4
   ```

2. **Environment gate**: move the secrets into an environment with required reviewers (OB-2) and keep the
   workflow unfiltered; Replicant pushes then wait for a human approval before any secret is released.
3. **Secret-free CI for Replicant branches**: run the test workflow without secrets for `replicant/**`
   (unit tests only), and keep integration or deploy workflows behind patterns 1 or 2.

Fork-based publishing (Replicant pushes to a fork, so GitHub's fork-PR protections apply) is the
recorded alternative for tenants that cannot change their CI; it is an open question for the user
(OQ-ORCH-01).

## Failure modes

| Situation | Effect |
|---|---|
| OB-1 not attested | Publish and repair pushes still need an approved digest when CI paths change (the gate is `critical`); takeover pushes touching CI paths are refused (archive and patch offered instead) |
| A tenant edits a workflow later to read secrets on `replicant/**` | The weekly lint re-run flags it to tenant admins and security; no automatic change |
| Repository with no branch rules (OB-3 missing) | UI warning on every job in that repo; the Replicant gate remains the only human control before merge |

## Interfaces

- **Provided:** checklist schema in `tenancy.tenants.onboarding_checklist`; onboarding screen; workflow lint view
  (data from WS-04's ecosystem-indexer).
- **Consumed:** WS-09 threat model T-PI-13 (R2-001), WS-04 workflow-derived CI path entries.
