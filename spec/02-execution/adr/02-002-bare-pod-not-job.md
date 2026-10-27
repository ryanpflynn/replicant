# ADR-02-002: The controller creates a bare Pod, not a Job

- **Status:** Accepted (WS-02, 2026-10-24). C-CRD §6 allows either; CCR-02-03 updates the C-IDS
  pod-name row.
- **Constraints:** D-04 (no duplicate runs), D-05, C-CRD ("at most once")

## Context

C-CRD recommends a Job with `backoffLimit: 0`. The observable contract is identical either way.
The controller already owns deadlines (from `agentStartedAt`), TTL, and failure classification.

## Options

1. `batch/v1 Job` with `backoffLimit: 0`, `podFailurePolicy`, `ttlSecondsAfterFinished`.
2. **Bare Pod named `ar-<body>` (chosen).**

## Decision

Option 2. The Pod name equals the AgentRun name, so a duplicate create returns `AlreadyExists`:
API-server name uniqueness gives a hard at-most-once guarantee for the agent process, combined
with the pod-creation marker (`Scheduled=Unknown/Waiting` written before creation) so a vanished
Pod is never recreated. The Job controller would add a second actor that creates Pods
asynchronously, with random names, and whose `activeDeadlineSeconds` semantics (from Job start)
differ from C-CRD's (from agent start).

## Consequences

- The controller implements the 1-hour TTL and the backstop deadline (`Pod.spec.activeDeadlineSeconds`).
- `podFailurePolicy` is not used; exit-code mapping lives in `controller.Derive`.
- C-IDS §2 says pods are `<job-name>-<5 chars>`; CCR-02-03 records `ar-<body>` for bare Pods.

## Revisit when

- Kubernetes adds a single-run primitive with stronger guarantees, or the team adopts
  `kubernetes-sigs/agent-sandbox` (v1.0.8, 2026-10-22 [verified: GitHub releases API]) as the
  substrate under the AgentRun contract.
