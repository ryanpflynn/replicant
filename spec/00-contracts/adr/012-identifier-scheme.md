# ADR-012: Prefixed lowercase Crockford base32 identifiers

- **Status:** Accepted (WS-00, 2026-10-24)

## Context

IDs flow through Temporal (workflow IDs ≤ 1,000 bytes, with some SQL configs at 255 [verified:
https://docs.temporal.io/cloud/limits, 2026-10-24]), Kubernetes (names ≤ 63 for Jobs, label values
≤ 63 [verified: https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/,
2026-10-24]), S3 keys, Loki metadata, Langfuse, URLs, Slack button values, and humans reading them
in UIs and logs.

## Options

1. **UUIDv4 / UUIDv7** (RFC 9562 [verified: https://www.rfc-editor.org/rfc/rfc9562.html,
   2026-10-24]). Native Postgres type. Not self-describing, 36 characters.
2. **Prefixed ULIDs and hash-derived IDs in one 26-character alphabet (chosen).**

## Decision

- Use `<prefix>_<26 lowercase Crockford base32>`: ULID for time-ordered IDs, `b32_130(sha256(…))`
  for derived IDs.
- Kubernetes names use the `ar-<body>` form because `_` is not allowed in names.
- Store as `text` with `CHECK` regexes.

## Consequences

- Self-describing (`job_`, `run_`, `gate_`), sortable where time-ordered, safe in every target
  system, and a single regex family.
- Slightly larger indexes than `uuid`. This is irrelevant at this scale.

## Revisit when

- Never expected.
