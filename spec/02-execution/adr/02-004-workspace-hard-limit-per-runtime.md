# ADR-02-004: Enforcing workspace hard limit per runtime

- **Status:** Accepted (WS-02, 2026-10-24); implements accepted ADR-014 (Challenge: D-07).
- **Constraints:** D-07 (amended), D-12

## Context

Kubelet `sizeLimit` is periodic eviction; FSQuotaMonitoring only monitors. C-CRD offers
`enforcement ∈ {watchdog, xfsProjectQuota, guestBlockDevice, fixedSizeVolume}`. Verified:
the AL2023 RAID0 helper mounts XFS without `prjquota`; Kata's `block-plain` emptyDir sizes the
image to the host filesystem, not `sizeLimit` (issue #2438); Kata runtime-rs uses a pre-existing
direct-volume `mountInfo.json` instead of creating its own image, and direct volumes are a
documented interface (`kata-ctl direct-volume add`); gVisor serves an unannotated emptyDir as a
host bind mount; containerd 2.x enables NRI by default and fails container creation when a plugin
errors.

## Options

1. Watchdog only (not a hard limit).
2. XFS project quotas for all runtimes (Kata via virtio-fs).
3. **Per runtime: Kata `guestBlockDevice` via an NRI-provisioned, exactly-sized direct block volume;
   gVisor `xfsProjectQuota`; overflow `fixedSizeVolume` (chosen).**
4. TopoLVM local volumes for all.

## Decision

Option 3, with our own node disk setup (RAID0, XFS `prjquota`), the NRI plugin
`replicant-workspace-nri` (fail-request policy), a fail-closed `workspace-guard` init container,
emptyDir `sizeLimit` above the hard limit so ENOSPC always precedes eviction, and the watchdog for
the structured `WorkspaceDiskExceeded` path.

## Consequences

- Guest-native ext4 for the hottest I/O path under Kata.
- A dependency on Kata's direct-volume interface; Kata upgrades are gated by B-DISK tests.
- Option 2 is the immediate fallback for Kata, option 4 for gVisor.

## Revisit when

- Kubernetes adds enforcing ephemeral-storage quotas, or Kata sizes block emptyDirs from `sizeLimit`.
