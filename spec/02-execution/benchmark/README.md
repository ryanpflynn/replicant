# Runtime Benchmark Harness (`sbxbench`)

**Owner:** WS-02 · **Gates:** [runtime-evaluation.md §8](../runtime-evaluation.md) (B-START, B-NPM, B-GO, B-CARGO, B-BUILD, B-QA, B-OVH, B-DENS, B-DISK, B-NET, B-NODE, B-MEM)
**Runs:** once as implementation spike S-1 on a dedicated staging exec cluster, then nightly (reduced
matrix) and on every Kata, gVisor, containerd, Cilium, kernel/AMI, or instance-family change.

## Purpose

Produce the evidence that sets or overturns the default runtime per workload class, the node pool
choice, `RuntimeClass.overhead`, workspace enforcement modes, and Kata memory request policy.

## Design

| Component | Description |
|---|---|
| `sbxbench` (Go CLI, `go/cmd/sbxbench`) | Drives the matrix. For each cell it stages inputs under a `replicant-eval` run prefix, signs a run grant with the staging grant key, and calls the **real Runner API** (`POST /v1/runs`) with a fixed `environment_definition` per workload, so the controller, NRI plugin, Cilium policy, and uploader paths are all exercised. It polls `GET /v1/runs/{id}` and collects phase timestamps |
| Workload images | `replicant/bench-<workload>@sha256:…`: environment images with the pinned repo snapshot baked as a git bundle and lockfiles; the agent container runs `bench-run <workload>` instead of a CLI (agent definition `bench-agent`, no gateway calls) except W-AGENT, which replays a recorded session through the gateway stub in replay mode |
| In-sandbox probe | `bench-run` records wall time per step, `getrusage`, `/proc/pressure/{cpu,io,memory}`, statfs, errno observed by writers (W-DISK), and writes `bench_result.json` into `/replicant/out` (uploaded as a manifest entry of type `eval_output` under the eval tenant) |
| Host probe (DaemonSet `bench-hostprobe`, staging only) | Samples every 5 s per sandbox pod cgroup: `memory.current`, `memory.peak`, `memory.stat` (anon/file), `cpu.stat`, `io.stat`; VMM/Sentry RSS by process; NVMe throughput. Exports `replicant_bench_*` metrics to Prometheus |
| Network probes (B-NET) | `bench-netprobe` image runs the 40 probes of [network.md §9](../network.md) and reports pass/fail per probe |
| Chaos (B-NODE) | terminates the EC2 instance of a node running 4 sandboxes (`aws ec2 terminate-instances`) and measures time to `NodeLost` |
| Report | `sbxbench report` writes `s3://<org>-replicant-staging-eval/runs/<eval_run_id>/bench/report.json` and a Markdown summary with every gate's measured value, threshold, and verdict; the verdicts feed the `runtime_selection` policy proposal |

## Matrix (full)

`runtime ∈ {kata-clh/guestBlockDevice, kata-clh/xfsProjectQuota, gvisor(directfs off)/xfsProjectQuota, runc (baseline)}`
× `node ∈ {m8id.24xlarge nested, c8id.24xlarge nested, m8id.metal-48xl, m8gd.24xlarge (gVisor/runc only)}`
× `concurrency ∈ {1, 8, 16, 24, 32}` × `image ∈ {cold, warm}` × workloads W-NPM, W-GO, W-CARGO, W-IMG,
W-IMG-B, W-QA, W-AGENT, W-DISK, W-START; 5 repetitions per cell (p50 and p95 reported); Kata memory
variants for B-MEM-01 (`reclaim_guest_freed_memory` off/on, guest `drop_caches` after bootstrap).

Nightly reduced matrix: m8id.24xlarge nested only, concurrency 1 and 16, warm image, W-NPM, W-GO,
W-DISK, W-START, B-NET.

## Commands

```bash
sbxbench run   --matrix full --env staging --runner https://runner.staging.replicant.internal --out s3://acme-replicant-staging-eval/runs/eval_01m5q2…/bench/
sbxbench run   --matrix nightly --env staging
sbxbench gates --report s3://acme-replicant-staging-eval/runs/eval_01m5q2…/bench/report.json   # exits 1 if any applicable gate fails
```

## Pass rules

A runtime/node pair becomes default for a class only if every applicable B-GATE passes
(runtime-evaluation.md §8.4); otherwise the fallback rules in §8.5 apply. Results are attached to
the `runtime_selection` policy change as evidence (WS-06 promotion requires it for runtime changes).
