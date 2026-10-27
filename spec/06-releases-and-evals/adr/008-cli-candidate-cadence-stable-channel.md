# ADR-06-008: Weekly CLI candidates from the vendor's stable channel

- **Status:** Accepted (WS-06, 2026-10-24)
- **Constraints:** D-10 (CLI versions change only through a Release Bundle), D-03

## Context

Claude Code published 40 releases from 2026-09-10 to 2026-10-23 and exposes `stable` (2.1.308) and `latest`
(2.1.310) npm dist-tags [verified: https://registry.npmjs.org/@anthropic-ai/claude-code, 2026-10-24]. Codex
published 25 releases from 2026-09-01 to 2026-10-23 and has only `latest` (0.164.0) [verified:
https://registry.npmjs.org/@openai/codex, 2026-10-24].

## Options

1. **Every release.** About 365 T2 evals per year per CLI (≈ $1.4M at 2× tokens) and a permanently blocked ramp slot.
2. **Monthly.** Cheap, but each candidate jumps about 30 releases (always `cli_minor`-class, so T3 and shadow),
   and security fixes wait.
3. **Weekly from `stable` (Codex: `latest` aged ≥ 72 h), with an expedited path for advisories (chosen).**

## Decision

Option 3 (upgrades.md §1). Candidates usually stay inside the `cli_patch` bounds (≤ 7 releases, ≤ 14 days), so
they are T2. A compatibility suite (C1–C9) gates every candidate before any eval spend.

## Consequences

- Production runs CLI versions 1–2 weeks behind `stable`.
- About 52 CLI candidates per year per CLI compete for the ramp slot with other releases.

## Revisit when

- Vendors publish a long-term-support channel, or Replicant's canary capacity grows (concurrent ramps).
