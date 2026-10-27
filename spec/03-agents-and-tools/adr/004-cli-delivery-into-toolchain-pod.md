# ADR-03-004: Delivering the pinned CLI into a toolchain pod

- **Status:** Accepted (WS-03, 2026-10-24). WS-02 implements in the pod template.
- **Constraints:** D-10 (CLI pinned by image digest), D-12 (toolchain from the environment definition), D-03

## Context

The CLI must run in the same container as the toolchain (it executes `go test`, `npm test`). The CLI is pinned by
`AgentDefinition.cliImage`; the toolchain by `EnvironmentDefinition.image.resolved`. They change independently
(CLI weekly, toolchains on their own cadence) and combine as roughly 6 CLIs-versions-in-flight × 30 environments.
Facts: Claude Code ships per-libc native binaries (`linux-x64`, `linux-x64-musl`, arm64 variants) [verified:
https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64-musl/latest, 2026-10-24]; Codex `linux-x64` ships a
static musl binary [verified: https://registry.npmjs.org/@openai/codex/-/codex-0.164.0-linux-x64.tgz, 2026-10-24];
Kubernetes image volumes are stable from 1.36 but need runtime support [verified:
https://kubernetes.io/docs/concepts/storage/volumes/#image, 2026-10-24].

## Options

1. **Combined images per (environment × CLI version)**, built by CI. Exact, but combinatorial builds and a digest per
   pair that neither definition owns.
2. **Install the CLI at start** (npm). Slow, needs registry egress, not pinned by image digest.
3. **Init-container copy (chosen):** the `prepare` init container runs from the CLI image and copies `/opt/replicant`
   into the `tools` emptyDir, mounted read-only into the agent container.
4. **Image volume** of the CLI image mounted at `/opt/replicant`.

## Decision

Option 3 now; option 4 as an optimization once WS-02 verifies image volumes under Kata and gVisor on the EKS AMIs.
The copy is about 250 MB from a locally cached image layer (well under a second on NVMe).

## Consequences

- CLI binaries must be self-contained: Claude Code's glibc and musl builds are both shipped; the entrypoint selects by
  probing the toolchain's loader. Codex is static.
- Pod startup adds one init container (on the critical path of the 90 s sandbox-ready target; budget 3 s).

## Revisit when

- WS-02 confirms image volumes on the runtime classes, or startup measurements show the copy above 3 s p95.
