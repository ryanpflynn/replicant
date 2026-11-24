# replicant-projector

Activity `projection.apply` on task queue `replicant-projections` (deployment `replicant-projector`, versioned): applies
ProjectionEvents (C-MSG 2.0.0, single `event` or batched `events`) to `jobs_read` and `approvals` per C-STATE §2.1 in
one transaction: tenant context for RLS, `job_timeline` ON CONFLICT DO NOTHING, seq-guarded upserts of `jobs`,
`job_runs`, `approvals.gates`, eligibility, decisions (from `decision_log`, with `text_sha256`), `job_prs`, memory usage,
steering, `audit.emit()` for gate and terminal events, and `NOTIFY replicant_job_updates '<tenant>:<job>:<seq>'`.
Also registers `qa.index_results` (acknowledged, not indexed in v0).

**In the stack, the worker runs inside the `core` process** (`control-plane-api serve` calls `cli.serve_in_process()`;
DEVIATIONS SIMPLIFY-D13). It keeps its own login, `REPLICANT_PROJECTOR_DB_DSN` / `_DB_PASSWORD_FILE`, which
`db.conninfo()` prefers over `REPLICANT_DB_*`, so it is still the only writer of `jobs_read` and `approvals`.
`replicant-projector run` still runs it standalone.

```bash
podman build -f images/replicant-py/Dockerfile --target dev -t localhost/replicant/replicant-py:dev .   # shared Python image (SIMPLIFY-D2)
replicant-projector run | healthcheck
uv run --package replicant-projector pytest workers/replicant-projector/tests   # starts wrk-pg-proj-<pid> (podman)
```
