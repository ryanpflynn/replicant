# ADR-009: A job's lifetime spans PR review and repair

- **Status:** Accepted (WS-00, 2026-10-24)
- **Constraints:** D-15 (bounded repair loops per job), D-19 (30-day waits), shared context §4

## Context

After Replicant opens a PR, reviewers comment, and Replicant repairs on the same branch
(D-15) with bounded loops **per job**. There are two ways to structure this:

1. The publishing job ends at "PR opened", and each review comment batch starts a new
   `pr_repair` job.
2. The job stays open in phase `pr_review` and runs repair cycles until the PR is merged, closed,
   or expires.

## Decision

Option 2 for PRs the Replicant opened:
- The workflow ID (`job_id`) stays the same.
- Each repair cycle starts with Continue-As-New, which bounds history.
- Feedback arrives as Signal `pr.event`.
- The repair budget (`repair_limits` policy) counts cycles per job.
- The PR wait is policy-bounded: it expires 30 days after the last activity by default, with
  reminders.
- `pull_request_merged` gives `succeeded`; `pull_request_closed` gives `rejected`.

A standalone `pr_repair` job (workflow type `pr_repair`) exists for a `/replicant` command on a PR the
Replicant did not open, or on a PR whose job has expired.

## Consequences

- One job shows the whole story: plan, approvals, PR, every repair, merge. This serves the "given a
  merged PR, find everything" question (WS-08).
- Jobs are long-lived. Version pinning and migration (ADR-006) and Continue-As-New per cycle are
  essential, not optional.
- The concurrency limit on open jobs per tenant must count `waiting_external` jobs separately from
  active ones (WS-01).

## Revisit when

- PR review waits routinely exceed 30 days, or reviewers want independent jobs per review round.
