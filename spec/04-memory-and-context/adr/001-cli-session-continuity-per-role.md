# ADR-04-001: CLI session continuity per role — handoff first, resume only within a cycle

- **Status:** Accepted (WS-04, 2026-10-24)
- **Constraints:** D-14 (working memory, session resume or handoff), D-07 (ephemeral disks), D-10 (native CLIs), D-21 (untrusted text)
- **Contracts:** C-ART (`handoff`, `cli_session_state` job37d raw-restricted), C-CRD (`SessionRestoreFailed`)

## Context

Every AgentRun has a fresh, ephemeral workspace. A later run of the same job can either restore the
CLI's own session files and `--resume`, or start fresh with a structured handoff. Facts:

- Claude Code stores sessions as JSONL under `$CLAUDE_CONFIG_DIR/projects/<project>/<id>.jsonl`;
  "the entry format is internal to Claude Code and changes between versions"; resume works from any
  directory since v2.1.223; `--fork-session` gives a new ID [verified:
  https://code.claude.com/docs/en/sessions and …/cli-reference, 2026-10-24].
- Codex stores rollouts under `$CODEX_HOME/sessions/YYYY/MM/DD/`, sometimes zstd-compressed;
  `codex exec resume <id>` [verified: github.com/openai/codex source, learn.chatgpt.com docs, 2026-10-24].
- Jobs span repair cycles separated by human review (hours to days), Continue-As-New, possible
  rebases, and possible bundle migration (a different CLI digest).

## Options

1. **Always resume** where a capture exists. Best continuity; ties correctness to an internal,
   version-specific format; carries all earlier raw tool output (including untrusted text) forward
   indefinitely; sessions grow until compaction; replay depends on raw, unredacted, short-lived
   state.
2. **Never resume; handoff only.** Deterministic, redacted, schema-validated, durable (2 years), and
   CLI-agnostic. Loses the agent's working context, which makes tight fix loops (QA failure → fix)
   slower and more expensive.
3. **Handoff always; resume only for implementer/repair re-invocations in the same cycle with an
   identical CLI image digest (chosen).**

## Decision

Option 3, per role: planner fresh (no capture); implementer and repair capture and may resume within
the same cycle (digest equal, ≤ 256 MiB, < 24 h); reviewer and QA fresh and ephemeral; every
producing role writes a handoff. A restore failure is non-retryable for that run, and the node is
re-invoked in handoff-only mode. Resume uses `--fork-session` and `--system-prompt-snapshot off` so
the capture stays immutable and the current (revocation-filtered) memory applies.

## Consequences

- Reproducibility and audit rest on the handoff, not on raw sessions.
- Tight QA-fix loops keep context; cross-cycle repairs start clean with the reviewer's comments, the
  prior diff, and the handoff.
- The entrypoint (WS-03) implements capture, restore checks (digest, tree SHA), and the mechanical
  handoff.

## Revisit when

- Evals show handoff-only repairs need materially more review rounds than same-cycle resumes would
  predict, or a CLI publishes a stable, versioned session export format.

## Amendment (wave 2, 2026-10-24, XD-FIX-R2-053)

The operative rule is **SR-1**, owned by WS-03 (03 cli-invocation.md §2.8): same job and cycle,
implementer or repair only, identical CLI image digest, capture < 24 h, ≤ 256 MiB, head tree match,
and no bundle migration since the capture. This ADR keeps the rationale; working-memory.md §4.2 and
other WS-04 text cite SR-1 instead of restating it. WS-03's earlier open-question recommendation of
cross-cycle resume (XD-04-16) is superseded by SR-1.
