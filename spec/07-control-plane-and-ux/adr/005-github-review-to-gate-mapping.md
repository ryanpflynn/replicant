# ADR-07-005: Replicant gates before the PR; GitHub reviews are feedback

- **Status:** Accepted (WS-07, 2026-10-24) · resolves the C-MSG open question "GitHub review → gate mapping"
- **Constraints:** D-19, D-15, D-18

## Context

D-19 lists GitHub PR reviews/labels as an approval channel. But the Replicant App cannot approve its own PR
(PR authors cannot approve their own pull requests [verified, approving-a-pull-request-with-required-reviews,
2026-10-24]), and branch protection already requires human approvals from people with write access after the
PR exists. Treating a GitHub "Approve" review as a Replicant gate decision would conflate two controls and
would publish before the Replicant gate in some flows.

## Options

1. Replicant gates only before the PR; GitHub reviews feed `pr.event` (repair loop).
2. Map GitHub "Approve" reviews by CODEOWNERS to `publish_approval` on draft PRs.
3. Opt-in check-run actions (`check_run.requested_action`) on a draft PR as the GitHub decision surface for
   `publish_approval`, plus `/replicant approve|reject|changes` issue commands for pre-PR gates.

## Decision (amended in the fix round, 2026-10-24: option 3 withdrawn)

Option 1, with `/replicant approve|reject|changes` issue commands as the GitHub decision surface for gates
before any push. **Option 3 is withdrawn**: a draft PR before approval pushes agent-written code, and GitHub
runs `push` and `pull_request` workflows for App-token pushes with repository secrets, so target CI would
execute unreviewed code (R2-001). Reviews never decide Replicant gates.

## Consequences

- GitHub users can decide Replicant gates without leaving GitHub (issue commands), with the same digest and first-wins rules.
- No Replicant code reaches GitHub before a gate that the risk rules require has been approved for that exact diff.
- Two independent human controls remain: the Replicant gate and GitHub required reviews.

## Revisit when

- Tenants require that a single GitHub review satisfy both controls (a compliance decision).
