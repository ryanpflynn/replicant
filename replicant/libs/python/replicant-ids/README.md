# replicant-ids

C-IDS 2.0.0 reference implementation (`spec/00-contracts/ids-and-naming.md`): lowercase Crockford base32,
monotonic ULIDs, derived IDs (`run_`, `gate_`, `dec_`, `tko_`, `art_`, `ntf_`), idempotency keys, Temporal
naming (namespace, task queues, the 14 custom search attributes), trace/span IDs (rules T-1, T-5),
`release_bundle_id` over RFC 8785 JCS, and the §12 validation regexes. Stdlib only.

```python
from replicant_ids import new_id, idempotency_key, logical_activity_id, run_id, job_trace_id
job = new_id("job")
rid = run_id("payments", idempotency_key(job, logical_activity_id("implementer", 0, 1, "run")))
```

Test (from the repo root): `uv run --package replicant-ids pytest libs/python/replicant-ids`
Vectors: `tests/vectors.json` (the spec's normative vectors; a Go port must reproduce them).
