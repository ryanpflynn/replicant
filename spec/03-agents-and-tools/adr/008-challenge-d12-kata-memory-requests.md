# ADR-03-008: Challenge: D-12. Memory requests at the median peak overcommit hosts under Kata

- **Status:** **Accepted** (orchestrator, 2026-10-24); D-12 amended in the shared context ("Under Kata, memory requests
  follow the measured host-side footprint and are set equal to the limit when the p50 footprint ratio is ≥ 0.8"),
  supported by WS-02 benchmark B-MEM-01. Implemented as formula rp-2 (`tools/rp1.py` `derive_rp2`, environment-definition.md §5.4).
- **Constraint challenged:** D-12, clause "Resource profiles derive from measured baselines per codebase type:
  requests at median peak, limits at p95 plus roughly 30% headroom, plus measured runtime overhead". Only the
  **memory request** under **VM-based runtimes (Kata with Firecracker or Cloud Hypervisor)** is challenged.

## Context and evidence

Requests at the median peak assume that a pod's host memory footprint tracks its working set, so pods on a node
rarely peak together and the kubelet can reclaim. Under Kata the host footprint is the VM's memory, and memory the
guest touches is not returned to the host by default:

- Kata's Cloud Hypervisor configuration ships `reclaim_guest_freed_memory = false` ("Enabling this will result in the
  VM balloon device having f_reporting=on set. Then the hypervisor will use it to reclaim guest freed memory... Default
  false") [verified: https://raw.githubusercontent.com/kata-containers/kata-containers/main/src/runtime/config/configuration-clh.toml.in, 2026-10-24].
- Kata contributors reported that freeing memory in the guest does not free it on the host, with free page reporting
  as the potential fix [verified (community log, not official docs): https://meetings.opendev.org/irclogs/%23kata-general/%23kata-general.2022-06-24.log.html, 2026-10-24].
- Even with free page reporting, Linux keeps free lists small in favor of page cache, so "most of the benefit... will
  be void in real world scenarios" unless the guest drops cache [verified (kernel mailing list): https://lkml.iu.edu/hypermail/linux/kernel/2001.2/09266.html, 2026-10-24].
- Replicant workloads are build-heavy (`npm ci`, `go build`, `next build`) and fill the guest page cache with
  dependency trees and build outputs; a 25–60 minute run touches most of its memory limit as cache.
- PodOverhead covers the VMM and guest kernel, not guest page cache [verified: https://kubernetes.io/docs/concepts/scheduling-eviction/pod-overhead/, 2026-10-24].

Effect at the D-12 numbers: the Next.js example requests 5,376 Mi and limits at 12 GiB. If the host footprint of a
long run converges toward the limit, a node scheduled to its memory requests is overcommitted by about 2.2×. Node
memory pressure then triggers kubelet eviction (`PodEvicted`, retryable, losing up to an hour of agent work) or host
OOM kills of VMMs (worse: `NodeLost`-like failures without finalization, C-CRD §1). This fails D-12's own goal and
"fail loudly" (D-07) at the 150-concurrent-run scale, where co-scheduled builds are common.

Not yet measured: the actual host footprint distribution under Replicant's workloads. That is why this ADR proposes
a measured switch rather than a blanket change.

## Options

1. **Keep D-12 as written** for all runtimes.
2. **Kata: memory request = memory limit** (Guaranteed-like memory; CPU stays at the median peak).
3. **Enable `reclaim_guest_freed_memory`** plus periodic guest cache drop (`echo 1 > /proc/sys/vm/drop_caches` from the
   Kata agent between bootstrap and CLI start) and keep D-12. Costs memory-access performance and needs WS-02 evidence.
4. **Measure, then decide per runtime class (proposed).**

## Proposed replacement text for D-12

> Resource profiles derive from measured baselines per codebase type and runtime class: CPU and ephemeral-storage
> requests at the median peak; memory requests at the median peak of the **host-side pod footprint** (which under
> VM-based runtimes is at least the guest's resident memory and, without free page reporting, approaches the memory
> limit); limits at p95 plus roughly 30% headroom; plus measured runtime overhead declared on the RuntimeClass.

## Is the replacement a strict superset?

No: for Kata without free page reporting it raises memory requests (fewer sandboxes per node). It was adjudicated and
accepted, so WS-03 now designs against the amended D-12: rp-2 selects `median_peak` for gVisor/runc and
`host_footprint` or `match_limit` for Kata from the recorded `hostFootprintBytesP50`, and CI recomputes it.

## Decision rule (adopted)

For each Kata runtime class, after the first 30-run baseline: if p50 of (host footprint peak ÷ memory limit) ≥ 0.8,
rp-2 emits `match_limit` for profiles on that class; otherwise `host_footprint` with the host footprint p50 as the request.

## Consequences

- If accepted, Kata node pools need about 1.5–2× the memory for the same concurrency (WS-02/WS-10 sizing).
- If rejected, WS-02 should enable option 3 and track `PodEvicted` and node memory pressure as a trigger.

## Revisit when

- WS-02's benchmark reports host footprint ratios, or Kata enables free page reporting by default.
