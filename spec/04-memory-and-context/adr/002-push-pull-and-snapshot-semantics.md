# ADR-04-002: Push vs pull, and snapshot semantics (pinned, subtract-only, as-of pull)

- **Status:** Accepted (WS-04, 2026-10-24)
- **Constraints:** D-14 (pinned per job, fast rollback), D-03 (reproducibility), ADR-013 (memory entry states are operational configuration)
- **Contracts:** C-IDS `memory_snapshot_id` (content-addressed over sorted entry refs + policy ref)

## Context

D-14 requires both "pinned per job for reproducibility" and "disable … effective for new jobs
immediately". Agents also need on-demand access to memory that does not fit the context window, and
episodic records are too untrusted and too many to inject.

## Options

1. Inject everything relevant at each node, fresh from live memory. Not reproducible; a job's
   behavior changes mid-flight.
2. Pin a full snapshot at start; never change it. Reproducible, but a poisoned entry keeps acting on
   long-running jobs for up to 30 days.
3. **Pin at start, subtract-only revocation at later assemblies; pull searches bounded by
   `pull_as_of`; every pull result logged (chosen).**

## Decision

- Snapshot = content-addressed set of push-eligible entry versions plus the policy ref (C-IDS
  formula). Created at first assembly; bound to the job with `pull_as_of`.
- Later assemblies call `mode=revocation_filter`: entries whose current state is no longer active
  are removed and a derived snapshot is recorded. Nothing is ever added mid-job (state is
  operational configuration and may only change *whether*, ADR-013).
- Pull (memory MCP) answers from the snapshot plus entries active as of `pull_as_of`, minus revoked
  entries; episodes are pull-only. Results are logged in `memory.retrievals` for replay.

## Consequences

- Reproduction uses the snapshot, the stored injection bytes, and the retrieval log.
- Rollback reaches new jobs at commit, running jobs at the next node, and pull calls immediately.

## Revisit when

- Long jobs routinely miss conventions activated after they started (then consider per-cycle refresh,
  already available as an eval-only policy mode).

## Amendment (wave-1 fix round, 2026-10-24)

- **Tenant in the snapshot hash (R1-096, CCR-04-04).** `msnap_ = b32_130(sha256(JCS({tenant_id,
  entries, policy})))`. Without the tenant, two tenants with the same eligible set (only org entries,
  or none) collided on the primary key and RLS hid the existing row from the second tenant.
- **No per-job data in a shared snapshot.** Holdout arms are per (job, entry) in
  `memory.holdout_assignments` and applied at selection (`injections.withheld_refs`); two jobs bound
  to one snapshot get independent arms.
- **Exact replay names the source job (R1-101, CCR-04-05).** `options.evaluation.memory_replay_job_id`
  lets the eval job reuse the source runs' recorded refs, order, and renderer version, verified by
  `rendered_sha256`, and the source job's recorded pull results.
