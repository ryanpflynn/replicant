"""Container engine of the local stack: Podman (podman + podman-compose) or Docker (docker + docker compose).

REPLICANT_CONTAINER_ENGINE=podman|docker picks one. Unset: the engine whose daemon answers, Podman first. The choice
is exported as REPLICANT_CONTAINER_CLI, so the test scripts and fixtures the CLI starts use the same binary
(`${REPLICANT_CONTAINER_CLI:-podman}`).

Both compose implementations label the stack's containers and volumes com.docker.compose.{project,service}; the
helpers below use only the Docker-compatible CLI surface (`inspect`, `ps -q`, `rm`), which both engines share.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

ENGINES = ("podman", "docker")
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def _answers(cli: str) -> bool:
    if not shutil.which(cli):
        return False
    try:
        return subprocess.run([cli, "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@functools.cache
def name() -> str:
    want = os.environ.get("REPLICANT_CONTAINER_ENGINE", "").strip().lower()
    if want:
        if want not in ENGINES:
            raise SystemExit(f"REPLICANT_CONTAINER_ENGINE must be podman or docker (got {want!r})")
        return want
    for cli in ENGINES:
        if _answers(cli):
            return cli
    for cli in ENGINES:  # installed but not running: the error comes from that engine's own command
        if shutil.which(cli):
            return cli
    raise SystemExit("no container engine found: install Podman (brew install podman podman-compose) or Docker")


def is_podman() -> bool:
    return name() == "podman"


def export() -> None:
    """Make subprocesses (test scripts, fixtures) use the same engine."""
    os.environ["REPLICANT_CONTAINER_CLI"] = name()


def cli(*args: str) -> list[str]:
    return [name(), *args]


def compose_bin() -> list[str]:
    if is_podman():
        for c in (os.environ.get("REPLICANT_COMPOSE_BIN"), shutil.which("podman-compose"),
                  "/opt/homebrew/bin/podman-compose"):
            if c and Path(c).exists():
                return [c]
        raise SystemExit("podman-compose not found (brew install podman-compose, or set REPLICANT_COMPOSE_BIN)")
    if os.environ.get("REPLICANT_COMPOSE_BIN"):
        return [os.environ["REPLICANT_COMPOSE_BIN"]]
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise SystemExit("docker compose not found (install Docker Desktop, or the compose plugin)")


def socket() -> str:
    """The engine's API socket as a bind-mount source, i.e. its path inside the VM that runs the containers.
    REPLICANT_CONTAINER_SOCKET overrides it (e.g. the Docker CLI driving Podman's Docker-compatible API)."""
    if os.environ.get("REPLICANT_CONTAINER_SOCKET"):
        return os.environ["REPLICANT_CONTAINER_SOCKET"]
    if is_podman():
        try:
            out = subprocess.run(["podman", "info", "--format", "{{.Host.RemoteSocket.Path}}"], capture_output=True,
                                 text=True, timeout=30, check=True).stdout.strip()
            return out.removeprefix("unix://") or "/var/run/docker.sock"
        except (OSError, subprocess.SubprocessError):
            return "/var/run/docker.sock"
    return "/var/run/docker.sock"  # Docker Desktop, Colima and Linux Docker all mount it there


def exists(kind: str, ref: str) -> bool:
    """kind: image | container | volume."""
    return subprocess.run(cli(kind, "inspect", ref), capture_output=True).returncode == 0


def inspect(kind: str, ids: list[str]) -> list[dict]:
    """`<kind> inspect` of ids (kind: container | image | volume); unknown ids are left out."""
    if not ids:
        return []
    out: list[dict] = []
    for i in range(0, len(ids), 100):
        r = subprocess.run(cli(kind, "inspect", *ids[i:i + 100]), capture_output=True, text=True)
        try:
            out += json.loads(r.stdout or "[]") or []
        except json.JSONDecodeError:
            pass
    return out


def container_ids(*filters: str) -> list[str]:
    args = [a for f in filters for a in ("--filter", f)]
    return subprocess.run(cli("ps", "-a", "-q", "--no-trunc", *args), capture_output=True, text=True).stdout.split()


def containers(*filters: str) -> list[dict]:
    """Normalised container records: Id, Name, Labels, State, ImageID, Mounts (volume names), Dependencies (Podman
    only: containers this one requires)."""
    out = []
    for c in inspect("container", container_ids(*filters)):
        out.append({"Id": c.get("Id", ""), "Name": str(c.get("Name", "")).lstrip("/"),
                    "Labels": (c.get("Config") or {}).get("Labels") or {},
                    "State": (c.get("State") or {}).get("Status", ""), "ImageID": c.get("Image", ""),
                    "Mounts": [m.get("Name", "") for m in c.get("Mounts") or [] if isinstance(m, dict)],
                    "Dependencies": c.get("Dependencies") or []})
    return out


def images() -> list[dict]:
    """Id and Names (repo tags) of every image."""
    ids = list(dict.fromkeys(subprocess.run(cli("images", "-q", "--no-trunc"), capture_output=True,
                                            text=True).stdout.split()))
    return [{"Id": im.get("Id", ""), "Names": im.get("RepoTags") or []} for im in inspect("image", ids)]


def volumes(*filters: str) -> list[dict]:
    args = [a for f in filters for a in ("--filter", f)]
    names = subprocess.run(cli("volume", "ls", "-q", *args), capture_output=True, text=True).stdout.split()
    return [{"Name": v.get("Name", ""), "Labels": v.get("Labels") or {}} for v in inspect("volume", names)]


def image_created(ref: str) -> float | None:
    """Build time of an image (epoch seconds), None when it does not exist."""
    im = inspect("image", [ref])
    if not im:
        return None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", str(im[0].get("Created") or ""))
    if not m:  # RFC 3339 with nanoseconds from both engines
        return 0.0
    return datetime.fromisoformat(m[1] + (m[3] or "Z").replace("Z", "+00:00")).timestamp() + float("0." + (m[2] or "0"))


def rm_containers(ids: list[str], *, force: bool = False, volumes_too: bool = False) -> subprocess.CompletedProcess:
    """Remove containers. Podman removes the containers that depend on them too (--depend: sandbox pods share the
    pause container's netns); Docker has no such flag, so a second pass removes what the first could not."""
    flags = [*(["-f"] if force else []), *(["-v"] if volumes_too else [])]
    if is_podman():
        return subprocess.run(cli("rm", *flags, "--depend", *ids), capture_output=True, text=True)
    r = subprocess.run(cli("rm", *flags, *ids), capture_output=True, text=True)
    if r.returncode != 0:
        left = [i for i in ids if exists("container", i)]
        if left:
            r2 = subprocess.run(cli("rm", *flags, *left), capture_output=True, text=True)
            return subprocess.CompletedProcess(r.args, r2.returncode, r.stdout + r2.stdout, r2.stderr)
    return r


def trim() -> str:
    """Give freed blocks back to the host. Podman: fstrim in the machine; Docker Desktop reclaims on its own."""
    if not is_podman():
        return "not needed (Docker reclaims disk space itself)"
    t = subprocess.run(["podman", "machine", "ssh", "sudo fstrim -a"], capture_output=True, text=True, timeout=600)
    return "done" if t.returncode == 0 else f"failed ({t.stderr.strip()[-200:]})"
