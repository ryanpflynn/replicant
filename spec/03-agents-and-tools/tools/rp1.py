"""Resource profile formulas rp-1 and rp-2 (normative reference implementation).

rp-2 (current) = rp-1 plus the Kata memory-request branch of amended D-12 (accepted ADR-03-008).

Owner: WS-03. Implements environment-definition.md section 5.4. The profile-rightsizer job
(usage-ingestor) and the config-repo CI both import this module; CI recomputes every
`derivation.method: measured` profile from its recorded stats and fails on any mismatch, so the
numbers in a published EnvironmentDefinition are always reproducible from their evidence.

Inputs are per-run PEAK statistics over the baseline window (see section 5.3 for how each peak is
measured):
  cpu:        p50 / p95 of the per-run maximum 60 s average CPU usage, in millicores
  memory:     p50 / p95 of the per-run peak memory (cgroup v2 memory.peak of the agent
              container's cgroup, read by the watchdog at exit), in bytes
  workspace:  p50 / p95 of the per-run peak workspace bytes (watchdog high-water mark)
  ephemeral:  p50 / p95 of the per-run peak total pod ephemeral storage (workspace + writable
              layer + logs + other emptyDirs; kubelet /stats/summary ephemeral-storage), in bytes

Runtime overhead (gVisor Sentry / Kata VMM and guest kernel) is NOT added here. WS-02 sets it on
the RuntimeClass as overhead.podFixed from the same baseline runs; the scheduler and kubelet add
it to the pod (PodOverhead, GA 1.24).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MI = 1024**2
GI = 1024**3

HEADROOM = 1.30            # D-12: "limits at p95 plus roughly 30% headroom"
CPU_REQUEST_STEP_M = 250
CPU_LIMIT_STEP_M = 500
MEM_REQUEST_STEP = 256 * MI
MEM_LIMIT_STEP = 512 * MI
DISK_STEP = 1 * GI
MIN_CPU_REQUEST_M = 500
MIN_MEMORY_REQUEST = 1 * GI
MIN_WORKSPACE = 10 * GI
# A-8 (XD-FIX-R1-036): profiles assert only sizeLimit <= limits.ephemeralStorage and add no margin of their own;
# the controller adds H + F + O when it renders the container limit (02-execution workspace-storage.md section 3).
EPHEMERAL_MARGIN_OVER_WORKSPACE = 0


def _ceil_to(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


@dataclass(frozen=True)
class Stats:
    cpu_m_p50: int
    cpu_m_p95: int
    mem_p50: int
    mem_p95: int
    ws_p50: int
    ws_p95: int
    eph_p50: int
    eph_p95: int


@dataclass(frozen=True)
class HostStats:
    footprint_p50: int


KATA_CLASSES = {"replicant-kata-clh", "replicant-kata-fc"}
KATA_MATCH_LIMIT_RATIO = 0.8


@dataclass(frozen=True)
class Profile:
    cpu_request_m: int
    cpu_limit_m: int
    mem_request: int
    mem_limit: int
    eph_request: int
    eph_limit: int
    workspace: int


def derive(s: Stats, memory_request_policy: str = "median_peak") -> Profile:
    cpu_req = max(MIN_CPU_REQUEST_M, _ceil_to(s.cpu_m_p50, CPU_REQUEST_STEP_M))
    cpu_lim = max(cpu_req, _ceil_to(s.cpu_m_p95 * HEADROOM, CPU_LIMIT_STEP_M))
    mem_lim = _ceil_to(s.mem_p95 * HEADROOM, MEM_LIMIT_STEP)
    if memory_request_policy == "match_limit":
        mem_req = mem_lim
    else:
        mem_req = min(mem_lim, max(MIN_MEMORY_REQUEST, _ceil_to(s.mem_p50, MEM_REQUEST_STEP)))
    workspace = max(MIN_WORKSPACE, _ceil_to(s.ws_p95 * HEADROOM, DISK_STEP))
    eph_lim = max(_ceil_to(s.eph_p95 * HEADROOM, DISK_STEP), workspace + EPHEMERAL_MARGIN_OVER_WORKSPACE)
    eph_req = min(eph_lim, _ceil_to(s.eph_p50, DISK_STEP))
    return Profile(cpu_req, cpu_lim, mem_req, mem_lim, eph_req, eph_lim, workspace)


def derive_rp2(s: Stats, runtime_class: str, host: HostStats | None = None) -> tuple[Profile, str]:
    """rp-2: D-12 as amended. Returns (profile, memoryRequestPolicy).

    gVisor/runc: identical to rp-1 (request = container p50 peak).
    Kata: request follows the host-side footprint p50; when footprint_p50 / limit >= 0.8 the request
    equals the limit, because Kata does not return guest-touched memory to the host by default.
    """
    base = derive(s, "median_peak")
    if runtime_class not in KATA_CLASSES:
        return base, "median_peak"
    if host is None:
        raise ValueError("rp-2 needs hostFootprintBytesP50 for Kata runtime classes")
    if host.footprint_p50 / base.mem_limit >= KATA_MATCH_LIMIT_RATIO:
        return Profile(base.cpu_request_m, base.cpu_limit_m, base.mem_limit, base.mem_limit,
                       base.eph_request, base.eph_limit, base.workspace), "match_limit"
    req = min(base.mem_limit, max(MIN_MEMORY_REQUEST, _ceil_to(host.footprint_p50, MEM_REQUEST_STEP)))
    return Profile(base.cpu_request_m, base.cpu_limit_m, req, base.mem_limit,
                   base.eph_request, base.eph_limit, base.workspace), "host_footprint"


def fmt_cpu(m: int) -> str:
    return str(m // 1000) if m % 1000 == 0 else f"{m}m"


def fmt_bytes(b: int) -> str:
    if b % GI == 0:
        return f"{b // GI}Gi"
    if b % MI == 0:
        return f"{b // MI}Mi"
    raise ValueError(b)


def render(p: Profile) -> dict:
    """Render to the EnvironmentDefinition resourceProfile fields (requests/limits/workspace)."""
    return {
        "requests": {"cpu": fmt_cpu(p.cpu_request_m), "memory": fmt_bytes(p.mem_request), "ephemeralStorage": fmt_bytes(p.eph_request)},
        "limits": {"cpu": fmt_cpu(p.cpu_limit_m), "memory": fmt_bytes(p.mem_limit), "ephemeralStorage": fmt_bytes(p.eph_limit)},
        "sizeLimit": fmt_bytes(p.workspace),
    }


def host_from_stats(d: dict) -> HostStats | None:
    return HostStats(d["hostFootprintBytesP50"]) if "hostFootprintBytesP50" in d else None


def from_derivation_stats(d: dict) -> Stats:
    return Stats(
        d["cpuMillicoresP50"], d["cpuMillicoresP95"],
        d["memoryBytesP50"], d["memoryBytesP95"],
        d["workspaceBytesP50"], d["workspaceBytesP95"],
        d["ephemeralBytesP50"], d["ephemeralBytesP95"],
    )


if __name__ == "__main__":
    print(render(derive(Stats(2100, 3600, 5583457484, 9556302233, 19327352832, 32749125632, 22011707392, 35433480192))))
