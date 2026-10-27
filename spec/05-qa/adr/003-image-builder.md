# ADR-05-003: Container image builds with rootless BuildKit in a dedicated `builder` sidecar, Kata only

- **Status:** Accepted (WS-05, 2026-10-24), conditional on WS-02 confirming properties P5/P8 of
  [runtime-assumptions.md](../runtime-assumptions.md)
- **Constraints:** D-16 ("build … container images where applicable, using a daemonless builder such
  as BuildKit rootless or buildah"), D-06, D-21

## Context

Verified facts:

- Rootless BuildKit needs user namespaces and either `seccomp=unconfined`, `apparmor=unconfined` and
  unmasked `/proc`, or `--oci-worker-no-process-sandbox`, which avoids the `/proc` requirement but
  "is discouraged" because build containers can kill processes in the daemon container; Kubernetes
  examples use it because Kubernetes has no `systempaths=unconfined` equivalent. The overlayfs
  snapshotter needs kernel ≥ 5.11, otherwise fuse-overlayfs or `native`. Network mode is always host
  [verified: https://github.com/moby/buildkit/blob/master/docs/rootless.md, 2026-10-24]. The
  Kubernetes example runs `buildctl-daemonless.sh build --frontend dockerfile.v0` as uid 1000 with
  seccomp and AppArmor `Unconfined` and `BUILDKITD_FLAGS=--oci-worker-no-process-sandbox` [verified:
  https://github.com/moby/buildkit/blob/master/examples/kubernetes/job.rootless.yaml, 2026-10-24].
  Current release v0.34.1 [verified: GitHub API, 2026-10-24].
- Buildah rootless in a pod uses `BUILDAH_ISOLATION=chroot`, the overlay driver on an emptyDir, and
  SETUID/SETGID capabilities with subuid/subgid ranges [verified:
  https://github.com/containers/buildah/blob/main/docs/tutorials/05-openshift-rootless-build.md,
  2026-10-24]. Current release 1.43.5 [verified: quay.io tags, 2026-10-24].
- Kaniko: Google archived the repository in June 2025; forks exist (Chainguard, osscontainertools)
  with restricted or unofficial images [verified: https://thenewstack.io/kaniko-lives-on-chainguard-forks-googles-dumped-tool/,
  2026-10-24].
- Sandboxes hold no registry push credentials (D-21).

## Options

1. **BuildKit rootless, daemonless (chosen default).** Dockerfile fidelity (the same frontend as CI
   and Docker), cache mounts, OCI output, mature rootless mode.
2. **Buildah rootless, chroot isolation (supported alternative).** Daemonless, Containerfile
   compatible, good for codebases already using Podman tooling.
3. **Kaniko.** Runs without user namespaces, but archived upstream with uncertain image
   availability. Rejected.
4. **Docker-in-Docker / dockerd in the sandbox.** A daemon with more privilege than needed.
   Rejected (and under gVisor only with handler flags).
5. **Build outside the sandbox (CI, a builder service).** Moves untrusted Dockerfile execution to
   a shared builder (cross-tenant risk) and breaks "QA builds what it tests". Rejected for v1.

## Decision

- **Rootless BuildKit v0.34.1 in a native sidecar named `builder`**, which is the one security
  exception WS-02's controller renders (only for `workload_class: qa_build`, only for a container named
  `builder` declared by the QA section, refused on `replicant-gvisor`; WS-02 controller.md §4.1, runner
  admission A-9). The agent container, which runs repo code, stays on the restricted profile. This
  is a per-run daemon inside the sandbox, not a Docker daemon; D-16's "daemonless" intent (no shared,
  privileged build daemon) holds. The `buildctl-daemonless.sh` form is not used because it would put
  the relaxed profile on the agent container.
- Builder container (image `qa.imageBuild.builderImage`, a Replicant build of
  `moby/buildkit:v0.34.1-rootless`):

  ```yaml
  - name: builder
    image: 123456789012.dkr.ecr.us-east-1.amazonaws.com/replicant/buildkit-rootless@sha256:df8732b47bae0d6c302fff4f034dc390145c00a7fde7b568698c3414a909de90
    restartPolicy: Always                       # native sidecar
    command: [rootlesskit, buildkitd]
    args: [--addr, unix:///run/buildkit/buildkitd.sock, --root, /workspace/.buildkit,
           --oci-worker-no-process-sandbox, --config, /etc/buildkit/buildkitd.toml]
    securityContext:
      runAsUser: 1000
      runAsGroup: 1000
      runAsNonRoot: true
      seccompProfile: {type: Unconfined}       # allowed by the WS-09 VAP for Kata qa_build
      capabilities: {drop: [ALL], add: [SETUID, SETGID]}   # allowed by the VAP
      allowPrivilegeEscalation: true           # REQUESTED (XD-05-16): setuid newuidmap/newgidmap
      readOnlyRootFilesystem: true
    resources:
      requests: {cpu: '1', memory: 1Gi}
      limits: {cpu: '4', memory: 4Gi}
    volumeMounts:
      - {name: workspace, mountPath: /workspace}          # build context read by the client; state in .buildkit
      - {name: qa-buildkit-run, mountPath: /run/buildkit}
  ```

  `/etc/buildkit/buildkitd.toml` (baked in the builder image) maps `docker.io`, `ghcr.io`, `gcr.io`,
  `quay.io`, `mcr.microsoft.com` to the Artifactory container repository
  `artifactory.acme.internal/docker-virtual` (anonymous read; path-style mirror, falling back to
  per-registry subdomain FQDNs if BuildKit's mirror handling rejects a path,
  `[unverified: BuildKit path-prefixed mirror behaviour]`; no ECR from the sandbox, R1-115); `RUN` steps use host networking
  (BuildKit rootless default) and therefore the pod's egress policy and curated package mirrors.
- The harness (agent container) drives it:

  ```bash
  buildctl --addr unix:///run/buildkit/buildkitd.sock build \
    --frontend dockerfile.v0 \
    --local context=/workspace/repo/. \
    --local dockerfile=/workspace/repo/deploy \
    --opt filename=Dockerfile --opt target=runtime --opt platform=linux/amd64 \
    --output type=oci,dest=/workspace/.qa/image.tar \
    --metadata-file /workspace/.qa/out/buildkit-metadata.json
  syft scan oci-archive:/workspace/.qa/image.tar -o spdx-json=/replicant/out/qa/sbom/image.spdx.json
  ```

  followed by the image secret scan (gitleaks rules pinned in the QA tools over the unpacked image
  filesystem → `secret_scan_report`, CCR-09-05).
- buildah 1.43 (`--isolation chroot`, overlay storage) is the alternative `builder` image for
  codebases that need it; same container exception.
- The image is never pushed from the sandbox; QA keeps digest, metadata, SBOM, and secret-scan report.
- Exact needs answered for WS-09 G-25: seccomp `Unconfined`, SETUID/SETGID, `allowPrivilegeEscalation:
  true`, `--oci-worker-no-process-sandbox` (no unmasked `/proc`), no `privileged`, no devices, no
  `hostUsers: false`.

## Consequences

- Image-building codebases pay the build in QA (1–5 min typical) and gain a verified image digest and
  SBOM per change.
- `--oci-worker-no-process-sandbox` lets build steps signal processes in the build daemon; the
  daemon is per-run and in the same trust domain as the repo code, so this changes nothing about the
  boundary that matters (the VM).
- `[unverified: rootless BuildKit with the overlayfs snapshotter inside the Kata 4.3 guest kernel, and
  its behavior if allowPrivilegeEscalation stays false (single-UID mapping only)]`; fallbacks:
  `--oci-worker-snapshotter=native`, then buildah with `vfs`; if WS-09 refuses escalation, QA skips
  image builds (`runtime_unsupported`) and CI's build remains the image gate.

## Revisit when

- Kubernetes user namespaces (`hostUsers: false`, stable since 1.36 [verified:
  https://kubernetes.io/docs/concepts/workloads/pods/user-namespaces/, 2026-10-24]) plus a runtime
  that supports them make `procMount: Unmasked` usable, removing the need for
  `--oci-worker-no-process-sandbox`.
- A tenant needs multi-platform builds (QEMU emulation inside the guest is slow; consider native
  arm64 QA pools).
