# WS-02: Execution, Isolation Runtime, and Workspace Storage

**Output directory:** `spec/02-execution/`
**Constraints most relevant:** D-04, D-05, D-06, D-07, D-08, D-12, D-21

## Purpose

Design everything on the execution side of the Runner API: the Runner service, the AgentRun
controller, the isolation runtime, sandbox node pools, workspace storage, network controls inside
and out, artifact collection, and the local implementations of the same contract.

## Required outputs

### 1. Runtime comparison and recommendation (`runtime-evaluation.md`)

Compare gVisor (systrap and KVM platforms), Kata + Firecracker, and Kata + Cloud Hypervisor for this
workload. Verify every operational claim against current docs and source. Cover:

- Isolation boundary and known escape classes.
- EKS operability: AMI or Bottlerocket support, node bootstrap, containerd shim configuration,
  RuntimeClass and `overhead`, instance requirements (bare metal vs EC2 nested virtualization;
  verify which instance families currently support nested virtualization, if any), managed node
  group vs Karpenter.
- Firecracker specifics under Kata: block-device rootfs and the devmapper snapshotter, virtio-fs
  availability, device hotplug limits.
- Performance: cold start, `npm ci` / `go build` / `cargo build` / image build throughput, memory
  overhead per sandbox, density per node.
- Rootless image builds (BuildKit rootless, buildah) and sidecar services under each runtime.
- emptyDir behavior and `sizeLimit` enforcement under each runtime (host-side vs guest-side writes,
  virtio-fs implications, whether kubelet accounting still sees usage).
- A **benchmark plan** the team can run (workloads, metrics, pass thresholds) to confirm the choice.
- Recommendation: default runtime per workload class (coding, QA/build), with an ADR. The design must
  keep the runtime swappable through RuntimeClass and policy.

### 2. Runner service (`runner-service.md`)

Implements `spec/00-contracts/runner-api.openapi.yaml`. Internals: authentication of callers,
idempotency on the AgentRun resource itself (for example a deterministic name or label derived
from the idempotency key; the Runner does not use the factory database, per D-23), translation of run spec → AgentRun object,
admission checks (quota, tenant policy, runtime class allowed), completion notification to Temporal,
status endpoint backed by the CRD, steering message delivery into a running sandbox (if the contract
supports it), cancellation.

### 3. AgentRun controller (`controller.md`)

- Reconcile loop in detail (kubebuilder/controller-runtime), with real Go type definitions for the
  spec/status structs matching the frozen CRD.
- Child objects: Job or bare Pod (decide), ServiceAccount, Secret projection for short-lived
  credentials, ConfigMaps for agent config, NetworkPolicy or CiliumNetworkPolicy.
- Pod template: RuntimeClass, security context (non-root, read-only root FS where possible, dropped
  capabilities, seccomp profile), init containers (workspace preparation, repo fetch), main container
  (CLI runner), sidecars (log/transcript shipper, artifact uploader, disk watchdog), resource requests
  and limits from the resource profile, `ephemeral-storage`.
- Artifact collection: how outputs are uploaded before exit (in-pod uploader with scoped,
  prefix-restricted S3 credentials vs controller-side collection; decide), upload verification,
  manifest writing, and what happens if the pod dies before upload.
- Terminal phase detection, completion event emission, finalizer cleanup, garbage collection of
  orphans, timeouts (activeDeadlineSeconds plus controller-enforced), and behavior when the
  controller restarts mid-run.
- Leader election, concurrency, rate limiting, and metrics.

### 4. Workspace storage (`workspace-storage.md`)

- emptyDir with `sizeLimit` on NVMe instance-store node pools: node configuration (kubelet root or
  ephemeral storage on NVMe, RAID0 across devices, filesystem choice), real pod spec YAML.
- Hard enforcement: XFS project quotas via `LocalStorageCapacityIsolationFSQuotaMonitoring` (verify
  current feature-gate status) or alternative, plus the in-sandbox watchdog that emits a structured
  `WorkspaceDiskExceeded` failure.
- Overflow: generic ephemeral volume spec with an EBS gp3 StorageClass (`reclaimPolicy: Delete`,
  `volumeBindingMode: WaitForFirstConsumer`, IOPS/throughput parameters). When the policy chooses it,
  attach-time cost, and per-node attachment limits.
- Explicitly document why CSI ephemeral inline volumes and EBS snapshots are rejected.
- Disk sizing method tied to the resource profile (WS-03 owns profile format).

### 5. Fast path (`fast-path.md`)

How sandbox-ready time stays under the p95 target: image pre-pull or lazy-loading snapshotters (for
example SOCI or stargz; verify compatibility with each runtime), base-image strategy, Git mirror and
reference clones or partial clones, read-only shared dependency caches (mount strategy, poisoning
prevention), warm node capacity via Karpenter. Give a cold-start budget broken down by phase.

### 6. Network (`network.md`)

Default-deny egress; allowed destinations (gateway, package mirror/proxy, artifact store prefix, MCP
context services); FQDN policy mechanism (Cilium vs egress proxy; decide with ADR); DNS controls;
no access to instance metadata (IMDS), the Kubernetes API, or other tenants' sandboxes; egress for QA
sidecars.

### 7. Local implementations (`local-runner.md`)

- **Compose profile:** a local Runner that satisfies the same OpenAPI contract with Docker or Podman
  (rootless Podman preferred where possible), the same container images, same mounts, same artifact
  upload to MinIO. Document every behavior that differs from production and why.
- **kind/k3d profile:** the real controller and CRD running locally, with Kata where KVM is
  available (Linux hosts; document macOS options and limits honestly).

## Must answer

- What does the sandbox contain at the moment the CLI starts, and how did each piece get there?
- What exactly happens when a node is lost mid-run?
- How many concurrent sandboxes fit on your recommended node type, per workload class?
- Which runtime facts in your design are unverified, and what is the fallback for each?

## Acceptance criteria

- Runtime recommendation backed by verified facts and a runnable benchmark plan.
- Real pod spec, CRD Go types, StorageClass, and network policy YAML.
- Every failure path in the run lifecycle has defined behavior.
- Local and production differences are listed exhaustively.
