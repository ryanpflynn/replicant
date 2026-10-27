# ADR-02-001: Default isolation runtime per workload class

- **Status:** Accepted (WS-02, 2026-10-24), conditional on the benchmark gates in
  [runtime-evaluation.md §8](../runtime-evaluation.md); fallbacks there apply automatically.
- **Constraints:** D-06, D-07, D-16, D-21

## Context

D-06 requires an evidence-based default among gVisor (systrap/KVM), Kata + Firecracker, and Kata +
Cloud Hypervisor, possibly per workload class, swappable through RuntimeClass and policy. Verified
facts (runtime-evaluation.md §1): Kata 4.3.1 with the Rust runtime is current; Cloud Hypervisor has
virtio-fs and hotplug; Firecracker needs a devmapper thin-pool per node, has no filesystem sharing,
and makes emptyDir guest-local where kubelet cannot enforce `sizeLimit`; gVisor's default platform
is systrap and its KVM platform is best on bare metal; EC2 nested virtualization is available on
M8id/C8id/R8id (Intel only) and Karpenter v1.13+ can enable it; rootless BuildKit needs unconfined
seccomp/AppArmor and Docker-in-gVisor needs added capabilities.

## Options

1. gVisor everywhere (no KVM requirement, Graviton possible).
2. Kata + Firecracker everywhere (smallest VMM, fastest boot).
3. **Kata + Cloud Hypervisor everywhere, gVisor as standby (chosen).**
4. Split: gVisor for coding/review/utility, Kata CLH for qa_build (two pools, two runtimes in
   steady-state operation).

## Decision

Option 3. All four classes default to `replicant-kata-clh` with `guestBlockDevice` workspaces on
nested-virt M8id/C8id/R8id pools. gVisor (systrap, directfs off) stays deployed, conformance-tested
weekly, and selectable per tenant and class by a `runtime_selection` policy version. Firecracker's
RuntimeClass exists but no policy selects it.

Option 4 is the documented fallback if Kata fails the coding-class gates while gVisor passes them.

## Consequences

- One runtime, one node family, one hardening profile in steady state; a real guest kernel for
  builds and sidecar services.
- Kata is amd64-only in kata-deploy for CLH and FC; Graviton savings are forgone.
- Escape risk concentrates in Kata/CLH/virtiofsd; mitigated by WS-09's hardening (no annotations,
  sandboxed virtiofsd, guest seccomp) and the gVisor standby.

## Revisit when

- Any B-GATE fails; a Kata or virtiofsd advisory without a timely fix; Kata gains sized block
  emptyDirs natively (issue #2438), which would remove the NRI provisioning dependency; boot latency
  becomes the bottleneck (re-evaluate Firecracker).
