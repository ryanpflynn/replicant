# ADR-08-004: Sandbox telemetry identity comes from Kubernetes metadata, never from content

- **Status:** Accepted (WS-08, 2026-10-24)
- **Constraints:** D-17 (every log line and trace carries the same `job_id`), D-21 (sandbox output is untrusted)

## Context

A sandbox runs untrusted repository code and a prompt-injectable agent with a shell. Anything it writes to stdout
or sends over OTLP can claim any `job_id`, `tenant_id` or `run_id`. If the pipeline trusted those values, a
sandbox could plant log lines or spans in another tenant's job (confusing operators, polluting the job view an
approver reads, or hiding its own activity).

The controller (WS-02) creates the pod and sets Kubernetes labels `replicant.example.com/{tenant,job-id,run-id,role,
workload-class,isolation,bundle}` (C-IDS section 8). The untrusted processes cannot change their pod's labels:
sandboxes have no Kubernetes API access (`automountServiceAccountToken: false`, C-CRD section 6).

## Options

1. Trust identity fields in content (simple, unsafe).
2. Authenticate each sender with its run token and compare claims (the collector would need JWT validation
   against the Runner JWKS; Alloy has no server-side JWT validator for OTLP receivers
   `[unverified: no generic JWT-validating server auth extension in Alloy 1.21]`).
3. **Derive identity from the pod (chosen):** logs by file path (node-logs.alloy reads
   `/var/log/pods/<ns>_<pod>_<uid>/<container>/`, so discovery labels identify the pod); OTLP by the
   connection's source IP (`otelcol.processor.k8sattributes` with `pod_association { source { from =
   "connection" } }`).

## Decision

Option 3. In node-logs.alloy the sandbox pipeline never extracts `job_id`, `run_id` or `tenant_id` from the line;
`trace_id` is recomputed from the label `job_id` with Rule T-1 (`sha256sum` in an Alloy template; verified to
reproduce the C-IDS test vector). Only the `uploader` container (UID 10003, which the agent's UID 10001 cannot
control, WS-02 controller.md section 4.2) may produce `source="transcript"`. In otlp-gateway-sandbox.alloy every
`replicant.*` identity attribute the sandbox sends is deleted, re-derived from pod labels, and telemetry from any
source that is not an `agentrun-controller`-managed pod in an `sbx-*` namespace is dropped. Both behaviors are
covered by tests (config/alloy/tests/).

## Consequences

- A sandbox can only pollute its own run's telemetry.
- Connection-based association needs the source IP seen by the gateway to be the pod IP (no SNAT between
  sandbox and gateway). This holds for in-cluster Service traffic under Cilium without egress gateway for that
  destination `[unverified: Kata and gVisor pod networking preserve the pod IP as the source address seen by an
  in-cluster Service; WS-02 to confirm in its runtime evaluation]`. Fallback: if association fails, telemetry is
  dropped (fail closed) and `otelcol_processor_dropped_spans` alerts; logs are unaffected.
- OTLP log fallback (B-LOG-01, R2-039): for a RuntimeClass whose stdout does not reach `/var/log/pods`, sandbox
  log lines arrive over OTLP. Identity (tenant, job, run, role, principal) is still taken from the pod by
  connection association, and claimed `sbx_*`/`replicant.*` attributes are stripped first. Only the container name
  (`replicant.container`) is sandbox-declared, so in fallback mode a compromised agent could label lines of its own
  run as `source="transcript"`; it still cannot write into another run or tenant. Tested by
  config/alloy/tests/test_otlp_gateway_sandbox.py.
- Local mode (Docker) cannot do connection association; local accepts sandbox-declared identity (documented
  parity gap, config/alloy/local.alloy header).

## Revisit when

- Alloy gains server-side JWT authentication for OTLP receivers; then also require the run token
  (audience to be added by WS-09) as defense in depth.
