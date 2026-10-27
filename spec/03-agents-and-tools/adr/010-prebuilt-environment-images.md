# ADR-03-010: Environment images are prebuilt by content hash; only repo-dependent steps run at start

- **Status:** Accepted (WS-03, 2026-10-24)
- **Constraints:** D-12 (tool definitions become images), D-03 (pinned behavior), D-21 (supply chain), SLO: sandbox
  ready p95 < 90 s

## Context

Tool definitions could become images in CI or be installed at sandbox start. Installs at start (apt, global npm,
Playwright browsers at about 150 MB) take tens of seconds to minutes, depend on mirror availability on the critical
path, and produce unscanned, unsigned, non-reproducible filesystems.

## Options

1. **Install everything at start.**
2. **Prebuild per environment definition, keyed by `sha256(JCS(image.build))` (chosen).**
3. **Prebuild per repository** (including dependencies). Fastest start, but one image per repo (1,500) and rebuilds on
   every lockfile change.

## Decision

Option 2, with `bakeFromRepo` as an opt-in for very large repos (option 3 selectively). Bootstrap steps that depend on
the checked-out commit (`npm ci`, `go mod download`, codegen) run at start against the package mirror.

## Consequences

- Images are built, scanned, signed, and attested once; definitions pin digests written by CI.
- Weekly base rebuilds create new environment versions and candidate bundles; WS-06 must keep that tier cheap.

## Revisit when

- Start-time bootstrap dominates sandbox-ready latency for a codebase type (then extend `bakeFromRepo`), or snapshot
  warm pools come into scope (out of scope for v1, D-06).
