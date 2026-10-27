# ADR-02-007: Sandbox node pools and warm capacity

- **Status:** Accepted (WS-02, 2026-10-24), conditional on B-GATE results.
- **Constraints:** D-06, D-07, D-22; ADR-007 (exec cluster, now its own AWS account per CCR-09-07)

## Context

Kata needs `/dev/kvm`: bare metal or EC2 nested virtualization (M8id/C8id/R8id and others, Intel
only, CPU option `NestedVirtualization=enabled`, no extra cost; AWS suggests evaluating metal for
performance-sensitive workloads). Karpenter v1.13+ exposes `cpuOptions.nestedVirtualization`.
Workspaces need local NVMe. Karpenter keeps no idle nodes, and node launch is too slow for the
sandbox-ready SLO.

## Options

A. **Nested-virt `m8id.24xlarge` (with `c8id`/`r8id` alternatives) via Karpenter (chosen default).**
B. `m8id.metal-48xl` (192 vCPU, 768 GiB, 3 × 3.8 TB NVMe).
C. Managed node groups with launch templates (nested virt honored since mid-2026).

## Decision

Option A, Karpenter NodePool `sbx-kata` (on-demand only, `do-not-disrupt` on running sandboxes,
custom AL2023 AMI with Kata, runsc, NRI plugin, and our disk setup), plus `sbx-gvisor` (min 0) and
`sbx-kata-metal` (benchmark and fallback). Warm capacity comes from negative-priority placeholder
Pods on the Kata RuntimeClass. Option B is the automatic fallback if nested-virt fails the B-GATEs.

## Consequences

- ≈ 8 nodes at the 150-run peak plus a 2-node reserve (runtime-evaluation.md §9, R1-052).
- Intel-only pools. Per-node blast radius equals the per-shape density in runtime-evaluation.md §9: 20 coding sandboxes on `m8id`/`r8id` 24xlarge, 13 on 16xlarge, 10 on `c8id.24xlarge`, 42 on the metal fallback.
- Pool weights: `sbx-kata` 10 (m8id, r8id), `sbx-kata-c8id` 5, `sbx-kata-metal` 1. Fallback rule 1 is a single change: set `sbx-kata-metal` to weight 20 (R1-276). The gVisor pool is amd64-only and sized for production peak so a runtime override can move all new runs (R1-277, R2-045).

## Revisit when

- B-GATE results; concurrency above ~1,000 runs (shard exec clusters, ADR-007).
