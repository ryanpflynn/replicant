"""`replicant dev prune`: keep the container VM's disk from filling (the Podman VM hangs when the host disk is full).

Default (safe): finished sandbox containers (ar-*, only runs with no container still running), exited one-shot
containers of project `replicant` (migrate, bootstraps: compose re-runs them on the next `up`), dangling images,
`fstrim -a` in the Podman machine (Docker reclaims space itself). --images: unused images except localhost/replicant/*:dev, the images the compose
files reference, the sandbox images and the test-runner images. --volumes: anonymous unused volumes and the volumes of
sandbox runs that have no container left. Named volumes of project `replicant` (Postgres, S3, Gitea, ...) are never
touched.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from replicant_cli import compose, engine
from replicant_cli.paths import Paths
from replicant_cli.testing import GO_IMAGE, NODE_IMAGE, PLAYWRIGHT_IMAGE

RUN_LABEL = "replicant.example.com/run-id"
SMOKE_IMAGES = ("docker.io/temporalio/admin-tools:1.32.1",)  # control-plane-api tests/smoke/env.sh
LOW_SPACE_GIB = 10


def free_gib() -> float:
    return shutil.disk_usage(Path.home()).free / 2**30


def _norm_ref(ref: str) -> str:
    if ref.startswith("localhost/") or "/" in ref.split(":")[0] and "." in ref.split("/")[0]:
        return ref
    return ("docker.io/" if "/" in ref.split(":")[0] else "docker.io/library/") + ref


def keep_images(p: Paths) -> set[str]:
    refs = {img for img, _, _ in compose.SANDBOX_IMAGES}
    refs |= {GO_IMAGE, NODE_IMAGE, PLAYWRIGHT_IMAGE, *SMOKE_IMAGES}
    for prof in ("v0,observability", "core,observability"):
        refs |= {s.image for s in compose.load_services(p, prof).values() if s.image}
    # image: lines the profile filter misses (other profiles, override-only services)
    env = compose.env_values(p)
    for f in p.compose_dir.glob("compose*.yaml"):
        for m in re.finditer(r"^\s*image:\s*[\"']?([^\s\"'#]+)", f.read_text(), re.M):
            refs.add(_norm_ref(compose.interpolate(m.group(1), env)))
    # base images of the repo's Dockerfiles, so `replicant dev up` rebuilds without pulling
    for f in [*p.root.glob("**/Dockerfile*")]:
        if "node_modules" in f.parts or ".venv" in f.parts:
            continue
        for m in re.finditer(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", f.read_text(errors="replace"), re.M | re.I):
            ref = m.group(1).split("@")[0]
            if "$" not in ref and ":" in ref:
                refs.add(_norm_ref(ref))
    return refs


def _sandbox_plan() -> tuple[list[str], set[str]]:
    """(containers to remove, run ids that end up with no container)."""
    cs = engine.containers(f"label={RUN_LABEL}")
    by_run: dict[str, list[dict]] = {}
    for c in cs:
        by_run.setdefault((c.get("Labels") or {}).get(RUN_LABEL, ""), []).append(c)
    rm, done = [], set()
    for run, group in by_run.items():
        if all(c.get("State") in ("exited", "created", "stopped", "configured", "dead") for c in group):
            rm += [c["Id"] for c in group]
            done.add(run)
    return rm, done


def _one_shots() -> tuple[list[str], list[str]]:
    """(removable, kept): exited-0 one-shots of the project that no other container requires. podman-compose
    creates dependents with --requires, so a one-shot a running service depends on stays (rm would need --depend);
    Docker records no such dependency."""
    shots = [c.name for n, c in compose.containers().items()
             if n in compose.ONE_SHOT and c.status in ("exited", "stopped") and c.exit_code == 0]
    if not shots:
        return [], []
    info = engine.containers()
    required = {d for c in info for d in c["Dependencies"]}
    by_name = {c["Name"]: c["Id"] for c in info}
    rm = [n for n in shots if by_name.get(n) not in required]
    return rm, [n for n in shots if n not in rm]


def _unused_images(p: Paths) -> list[tuple[str, str]]:
    keep = keep_images(p)
    used = {c["ImageID"] for c in engine.containers()}
    out = []
    for im in engine.images():
        names = im["Names"]
        if not names or im["Id"] in used:
            continue  # dangling ones go with the default prune
        if any(n in keep or (n.startswith("localhost/replicant/") and n.endswith(":dev")) for n in names):
            continue
        out.append((im["Id"], ", ".join(names)))
    return out


def _unused_volumes() -> list[str]:
    """Anonymous volumes, and volumes of sandbox runs with no container left. A volume still mounted is refused by
    `volume rm` (no -f), so a race with a starting run fails safe."""
    cs = engine.containers()
    live_runs = {(c.get("Labels") or {}).get(RUN_LABEL) for c in cs} - {None}
    in_use = set()
    for c in cs:
        in_use.update(c["Mounts"])
    out = []
    for v in engine.volumes():
        name, labels = v["Name"], v["Labels"]
        if labels.get(engine.PROJECT_LABEL) == compose.PROJECT or name.startswith(f"{compose.PROJECT}_"):
            continue  # named project volumes: never
        anonymous = bool(re.fullmatch(r"[0-9a-f]{64}", name)) and not labels
        finished_run = labels.get(RUN_LABEL) and labels[RUN_LABEL] not in live_runs
        if (anonymous or finished_run) and name not in in_use:
            out.append(name)
    return out


def _rm(cmd: list[str], ids: list[str], log: Callable[[str], None], what: str) -> None:
    removed = 0
    for i in range(0, len(ids), 50):
        r = subprocess.run([*cmd, *ids[i:i + 50]], capture_output=True, text=True)
        removed += len([x for x in r.stdout.split() if x])
        if r.returncode != 0:
            log(f"  {what}: {r.stderr.strip()[-300:]}")
    log(f"  removed {removed} {what}")


def run(p: Paths, *, images: bool, volumes: bool, yes: bool, confirm: Callable[[str], bool],
        log: Callable[[str], None]) -> int:
    before = free_gib()
    log(f"host free space: {before:.1f} GiB")
    rm, done = _sandbox_plan()
    log(f"sandbox containers of finished runs: {len(rm)} ({len(done)} runs)")
    if rm:
        r = engine.rm_containers(rm)
        log(f"  removed {len(r.stdout.split())} sandbox containers" + (f" ({r.stderr.strip()[-300:]})" if r.returncode else ""))
    shots, kept = _one_shots()
    log(f"exited one-shot containers: {', '.join(shots) or 'none'} to remove"
        + (f"; kept (required by running services): {', '.join(kept)}" if kept else ""))
    if shots:
        _rm(engine.cli("rm"), shots, log, "one-shot containers")
    r = subprocess.run(engine.cli("image", "prune", "-f"), capture_output=True, text=True)
    log(f"  removed {len(re.findall(r'[0-9a-f]{64}', r.stdout))} dangling images")
    if images:
        un = _unused_images(p)
        log(f"--images: {len(un)} unused images (kept: localhost/replicant/*:dev, compose, Dockerfile FROM, sandbox, test images)")
        for _, n in un:
            log(f"    {n}")
        if un and (yes or confirm(f"remove {len(un)} images?")):
            _rm(engine.cli("rmi"), [i for i, _ in un], log, "images")
    if volumes:
        un = _unused_volumes()
        log(f"--volumes: {len(un)} unused anonymous or finished-run volumes (project volumes are never removed)")
        if un and (yes or confirm(f"remove {len(un)} volumes?")):
            _rm(engine.cli("volume", "rm"), un, log, "volumes")
    log(f"give freed space back to the host: {engine.trim()}")
    after = free_gib()
    log(f"host free space: {before:.1f} GiB -> {after:.1f} GiB ({after - before:+.1f})")
    return 0


def auto(p: Paths, log: Callable[[str], None], *, threshold_gib: float = LOW_SPACE_GIB) -> bool:
    """Below threshold_gib free on the host: the safe default prune plus orphan volumes (anonymous unused and
    finished-run volumes; project volumes never), without asking. Returns True when it pruned."""
    free = free_gib()
    if free >= threshold_gib:
        return False
    log(f"host free space is {free:.1f} GiB (< {threshold_gib} GiB): auto-pruning (`replicant dev prune --volumes`; "
        "the Podman VM hangs when the disk fills)")
    run(p, images=False, volumes=True, yes=True, confirm=lambda _q: True, log=log)
    left = free_gib()
    if left < threshold_gib:
        log(f"WARNING: still {left:.1f} GiB free after the prune; try `replicant dev prune --images`")
    return True
