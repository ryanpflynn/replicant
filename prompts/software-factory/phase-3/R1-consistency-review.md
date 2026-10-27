# R1: Consistency and Conformance Review

**Output:** `spec/90-reviews/R1-consistency.md`

## Purpose

Find every place where the specification contradicts itself, drifts from the frozen contracts, or
leaves a gap between workstreams. You are not redesigning; you are finding defects precisely
enough that the owner can fix them without asking you anything.

## Method

1. Read `01-shared-context.md`, all of `spec/00-contracts/`, and every Phase 2 section in full.
2. Check, at minimum:
   - **Contract conformance**: every use of the Runner API, AgentRun fields, signals/updates, IDs,
     artifact keys, Postgres schemas, and registry objects matches the frozen contract exactly
     (field names, types, phases, condition types).
   - **State ownership**: no section stores state in a place `state-ownership.md` does not assign
     it, and no section reads state through a path the matrix does not allow.
   - **Cross-workstream assumptions**: every entry in `cross-ws-dependencies.md` is satisfied by
     the named workstream.
   - **Decided constraints**: no section violates a D-xx without a Challenge ADR.
   - **Naming**: metric names, Loki labels and metadata keys, search attributes, and schema names are
     consistent everywhere.
   - **Lifecycle alignment**: AgentRun phases, job states, memory states, gate states, and bundle
     promotion states line up wherever one feeds another.
   - **Runtime alignment**: WS-05's QA dependency strategy, WS-03's images, WS-02's runtime
     recommendation, and WS-11's local profiles agree.
   - **Syntax**: YAML, JSON, SQL, OpenAPI, and code blocks parse. Run validators where available.
3. Check that every "Must answer" question in every brief is actually answered.

## Output format

A table of findings: ID (`R1-NNN`), severity (blocker, major, minor), locations (file and heading),
the exact inconsistency, the contract or constraint involved, and the suggested owner. Then a list of
unanswered "Must answer" questions per workstream.
