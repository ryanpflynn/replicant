# ADR-02-003: In-pod uploader; credentials only in replicant-agentd

- **Status:** Accepted (WS-02, 2026-10-24), revised after WS-09 control review G-01/G-22 and the
  accepted CCR-09-01 (per-audience tokens) and CCR-09-02 (run grants).
- **Constraints:** D-05, D-08, D-21; C-ART §5; C-CRD §5.2

## Context

Artifacts must be committed before a run may be `Succeeded`, and the brief asks to decide between
an in-pod uploader with prefix-scoped credentials and controller-side collection. The agent is
untrusted and prompt-injectable; any credential it can read is exfiltratable. WS-03 placed all
run tokens in the `replicant-agentd` sidecar.

## Options

1. Controller-side collection (exec into the pod, or pod pushes to the controller). Needs exec into
   untrusted sandboxes (forbidden, WS-09 G-26) or an inbound path to the controller.
2. Agent container uploads with its own credentials. The agent could forge or pre-empt the
   manifest and exfiltrate credentials.
3. **Trusted uploader container in the Pod; credentials only in agentd; S3 sessions passed to the
   uploader on a private socket (chosen).**

## Decision

The `uploader` is a regular container (uid 10003) that reads `out` and `spool` read-only, ships
spool chunks during the run, uploads outputs after the agent finishes, verifies each object
through S3 SHA-256 checksums, and writes `manifest.json` last with `If-None-Match: *`. It obtains
STS session credentials from `replicant-agentd` over a unix socket in a volume mounted only into
agentd and the uploader. The controller re-verifies the manifest and object sizes/checksums with
`HeadObject` before `Succeeded`.

## Consequences

- The agent cannot write to S3, cannot forge a manifest, and holds no credential.
- The uploader is a regular container (not a native sidecar), because sidecars are terminated
  after main containers exit; WS-03's request for a sidecar uploader is answered in XD-02-01.
- Pod death before finalization leaves only streamed chunks (C-CRD rule 2).

## Revisit when

- S3 can verify core-signed run grants in session policies (then bind STS sessions to grants,
  closing WS-09 residual R-04).
