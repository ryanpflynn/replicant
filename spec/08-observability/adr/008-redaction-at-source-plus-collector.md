# ADR-08-008: Redaction at the source, again at the collector, and a post-hoc scanner

- **Status:** Accepted (WS-08, 2026-10-24). WS-09 reviews the ruleset.
- **Constraints:** D-21 ("Transcripts and logs are redacted for secrets and PII before reaching Loki or
  Langfuse"), C-ART redaction column

## Context

Untrusted content enters transcripts from repo files, tool output, issue text and model output. Sinks: S3
`transcripts` and `artifacts` buckets, Loki, Langfuse (ClickHouse plus its S3 event bucket), Tempo. A single
redactor is a single point of failure; a redactor inside the agent's trust domain can be bypassed by the agent.

Facts: the sandbox pod's `uploader` container runs as a different UID from the agent and holds the only S3 write
credentials (WS-02 controller.md section 4.2). Alloy's `loki.secretfilter` is GA, uses gitleaks rules and accepts
a custom gitleaks config [verified: Alloy v1.21.2 docs, loki.secretfilter.md, 2026-10-24]. gitleaks v8.30.1 is
MIT-licensed and exposes `detect.NewDetector(cfg)` and `DetectString` for embedding [verified:
github.com/gitleaks/gitleaks v8.30.1 detect/detect.go and LICENSE, 2026-10-24]. Default gitleaks rules missed a
run-token-shaped JWT, a bearer header and an AWS key outside the base32 alphabet in a local test (2026-10-24).

## Options

1. Redact only in the collector (Alloy). Misses S3 (the sandbox uploads directly).
2. Redact only in the sandbox. One layer; a ruleset gap leaks everywhere.
3. **Three layers with one ruleset (chosen).**

## Decision

1. **Source (primary):** the uploader redacts every `required` artifact (C-ART section 4) and every transcript
   event before upload, before writing transcript lines to its stdout, and before emitting OTLP observations. It
   embeds gitleaks with `redaction/gitleaks-replicant.toml`, the PII rules, the entropy detector and exact-match
   redaction of known credentials (pii-rules.yaml). Failure mode: **fail closed**. A redactor error drops the
   affected event or chunk, writes a placeholder `{"redaction_error": "<rule engine error code>"}`, and counts
   `replicant_redaction_errors_total`.
2. **Collector (second pass):** Alloy `loki.secretfilter` on every log line in both clusters with the same TOML;
   OTTL `replace_all_patterns` on span attributes at the sandbox OTLP gateway; Tempo-bound spans lose content
   attributes entirely.
3. **Post-hoc (detection):** `leak-scanner` scans new S3 transcript and artifact objects (S3 Event
   Notifications to SQS), samples Loki with LogQL, and queries Langfuse observations; a finding pages security and
   starts the purge runbook (session-capture.md section 8).

The ruleset is versioned (`redact-YYYY.MM.N`), lives in the replicant-config repo under `observability/redaction/`,
is conformance-tested in CI (redaction/tests/), ships in the uploader image (WS-02) and as ConfigMap
`replicant-redaction-ruleset` for Alloy and leak-scanner. It is operational configuration (ADR-013): it changes
what is stored, never how an agent behaves, so it is not part of a Release Bundle.

## Consequences

- Platform services redact at the source too (R1-198): Python services through `replicant_telemetry.JsonLogFormatter`
  and code/redact.py (Replicant rules, PII rules, known values; fail closed), Go services through the uploader's
  redactor as an `slog` handler. Third-party producers (Kubernetes events, Hubble) get the collector pass only.
- Purge scope follows C-ART 1.9.0 rule 4a (R2-005): a miss is purged from `artifacts`, `transcripts`, `session`,
  Loki, Langfuse, Tempo and the read models; the Object-Locked `audit` bucket never holds free text, so it is
  never purged and no role holds `s3:BypassGovernanceRetention` (session-capture.md section 8).

- Second-pass hits (`loki_secretfilter_secrets_redacted_by_category_total` on sandbox origins) mean the first
  layer missed a pattern: alert `ReplicantSecondPassRedactions`.
- CPU: secretfilter costs CPU per line; it runs after volume reduction, with `processing_timeout = 250ms` and
  forwarding (not dropping) on timeout so that a slow line is still caught by the post-hoc scanner.

## Revisit when

- A redaction miss reaches a sink that cannot be purged (live Temporal history of an open job;
  session-capture.md section 8.1).
