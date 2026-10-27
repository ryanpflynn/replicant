# ADR-04-005: Preferences only at team scope; no per-reviewer profiles

- **Status:** Accepted (WS-04, 2026-10-24); product confirmation requested (open question)
- **Constraints:** D-14 (brief asks whether preferences are allowed at all), D-25

## Context

Reviewers express style preferences ("prefer table-driven tests"). Per-person memory would let the
Replicant tailor PRs to whoever reviews, but it builds behavioral profiles of employees, makes output
depend on reviewer assignment, and attributes conventions to individuals.

## Options

1. No preferences. Loses real team conventions that are not correctness rules.
2. Per-reviewer preferences. Profiling, privacy, and fairness concerns; review-assignment coupling.
3. **Team-scoped preferences (service group, repo, path) with ≥ 2 distinct authors before activation;
   lowest precedence; admin approval (chosen).**

## Decision

Option 3. `memory_type = 'preference'` is allowed only at `service_group`, `repo`, `path` (DB CHECK).
Non-admin views never show who expressed it.

## Consequences

- Style conventions shared by a team are learned; individual quirks are not.

## Revisit when

- Teams ask for per-reviewer behavior and HR/legal approve it.
