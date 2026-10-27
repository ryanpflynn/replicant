# ADR-014: Challenge: D-07. FS quota *monitoring* is not a hard limit

- **Status:** Proposed challenge (WS-00, 2026-10-24). The orchestrator adjudicates. WS-02 designs the
  mechanism.
- **Constraint challenged:** D-07, the clause "Design for both: XFS project quotas
  (`LocalStorageCapacityIsolationFSQuotaMonitoring`) or an equivalent hard limit, plus an
  in-sandbox disk watchdog".

## Context and evidence

D-07 correctly notes that kubelet `sizeLimit` enforcement is periodic eviction, not a hard quota
[verified: https://kubernetes.io/docs/concepts/storage/ephemeral-storage/, 2026-10-24]. It then
names `LocalStorageCapacityIsolationFSQuotaMonitoring` as a way to obtain a hard limit. That is
false:

- The Kubernetes documentation says: "Project quotas let you monitor storage use; **they do not
  enforce limits**" [verified: https://kubernetes.io/docs/concepts/storage/ephemeral-storage/,
  2026-10-24]. The feature makes usage accounting faster and more accurate, including deleted but
  still open files. Overrun is still handled by eviction.
- The feature gate is beta since 1.31 and **disabled by default** [verified: same page,
  2026-10-24].
- It requires XFS (or ext4 with project quotas), and the **pod must run in a user namespace**
  (`hostUsers: false`) [verified: same page, 2026-10-24]. Interaction with Kata (guest kernel) and
  gVisor (gofer and overlay) is undocumented, so emptyDir writes under those runtimes may not be
  visible to the host quota as expected `[unverified: behavior of FSQuotaMonitoring under Kata and
  gVisor; WS-02 benchmark]`.

This is the same class of error as the earlier EBS inline-volume claim. A design that relied on
this gate for "fail loudly on overrun" would neither enforce the limit nor fail loudly. The pod
would be evicted on the next kubelet scan, which is exactly the failure D-07 wants to avoid.

## Options (replacement)

1. **Guest block device under Kata.** The workspace is a fixed-size block device or a sized
   filesystem inside the VM, so writes past the limit get ENOSPC inside the guest. A hard limit,
   whose failure is visible to the build tool and the watchdog.
2. **Enforcing XFS project quota set by a node agent.** A privileged DaemonSet or CSI driver sets
   `xfs_quota -x -c 'limit -p bhard=<size> <projid>'` on the emptyDir backing directory, on XFS
   mounted with `prjquota`. A hard limit at host level, for gVisor and runc.
3. **Fixed-size generic ephemeral volume** (EBS gp3, already the D-07 overflow path). Size is the
   hard limit, at the cost of attach time.
4. **Loopback-file or LVM thin volume per pod via a local CSI driver** (for example TopoLVM). A hard
   limit with local NVMe performance.

## Proposed replacement text for D-07

> "Fail loudly on overrun": kubelet `sizeLimit` enforcement is periodic eviction, not a hard quota,
> and `LocalStorageCapacityIsolationFSQuotaMonitoring` only improves accounting (it does not enforce).
> Design for both: **an enforcing hard limit per workspace** (guest block device under Kata; enforcing
> XFS project quota `bhard` set by a node agent, or a per-pod LVM/loop volume, under gVisor/runc;
> volume size for generic ephemeral volumes), **plus** an in-sandbox disk watchdog that emits a
> structured `WorkspaceDiskExceeded` failure before eviction.

## Is the replacement a strict superset?

Yes. It keeps the watchdog, the sizeLimit, the NVMe default, and the overflow path, and replaces a
mechanism that does not do what D-07 intends with ones that do. Per shared-context §3, WS-00 is
designing against the replacement: AgentRun `spec.workspace.enforcement` ∈ {`watchdog`,
`xfsProjectQuota`, `guestBlockDevice`, `fixedSizeVolume`}, where `xfsProjectQuota` means the
enforcing variant. WS-02 picks per runtime class.

## Consequences

- WS-02 must verify each mechanism under each runtime in its benchmark plan. It must decide whether
  the FSQuotaMonitoring gate is still worth enabling for accounting accuracy, which requires user
  namespaces.
- The failure reason `WorkspaceDiskExceeded` is reached via `Finalizing`: the watchdog or ENOSPC is
  detected, then partial artifacts are uploaded. That is better evidence than an eviction.

## Revisit when

- Kubernetes adds enforcing ephemeral-storage quotas.
