"""Compose wrapper for the local stack (stack.md §4: shells out to podman-compose or docker compose, project
`replicant`; engine.py picks the engine)."""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from replicant_cli import engine
from replicant_cli.paths import Paths

PROJECT = "replicant"
ONE_SHOT = frozenset({"migrate", "s3-init", "temporal-bootstrap", "gitea-bootstrap", "temporal-schema", "k8s-provisioner"})
OWNERS = {
    "postgres-replicant": "PLATFORM", "temporal": "PLATFORM", "temporal-bootstrap": "PLATFORM", "temporal-ui": "PLATFORM",
    "s3": "PLATFORM", "s3-init": "PLATFORM", "migrate": "PLATFORM", "gitea": "PLATFORM", "gitea-bootstrap": "PLATFORM",
    "control-plane-api": "CONTROL", "run-grants": "CONTROL", "github-integration": "CONTROL",
    "github-token-broker": "CONTROL", "replicant-worker": "WORKER", "publisher-worker": "WORKER",
    "replicant-projector": "WORKER", "runner-local": "RUNNER", "repo-mirror": "RUNNER", "gateway-shim": "GATEWAY",
    "gateway-state": "GATEWAY",
}
# Images runner-local starts sandboxes from (RUNNER-06/07). They are not compose services, and runner-local never
# pulls `localhost/` images, so `replicant dev up` builds them whenever runner-local is started.
# (image, dockerfile relative to the repo root, target or None); context = repo root.
SANDBOX_IMAGES = (
    ("localhost/replicant/pause:3.10", "go/cmd/sandbox-sidecars/Dockerfile", "pause"),
    ("localhost/replicant/sandbox-sidecars:dev", "go/cmd/sandbox-sidecars/Dockerfile", "sidecars"),
    ("localhost/replicant/cli-claude-code:dev", "images/cli-claude-code/Dockerfile", None),
    ("localhost/replicant/env-go-service:dev", "images/env-go-service/Dockerfile", None),
    ("localhost/replicant/env-python-service:dev", "images/env-python-service/Dockerfile", None),  # design §8c
    ("localhost/replicant/env-node-service:dev", "images/env-node-service/Dockerfile", None),
)
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")


class _Loader(yaml.SafeLoader):
    pass


for _tag in ("!reset", "!override"):
    _Loader.add_constructor(_tag, lambda loader, node: (
        loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_scalar(node)))


def env_values(p: Paths) -> dict[str, str]:
    vals: dict[str, str] = {}
    if p.env_file.is_file():
        for line in p.env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    vals.update({k: v for k, v in os.environ.items() if k.startswith("REPLICANT_")})
    return vals


def interpolate(s: str, env: dict[str, str]) -> str:
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), s)


@dataclass
class Service:
    name: str
    profiles: list[str]
    image: str
    build: dict | None
    depends_on: list[str] = field(default_factory=list)
    dockerfile: Path | None = None
    context: Path | None = None

    @property
    def replicant_image(self) -> bool:
        return self.image.startswith("localhost/")

    @property
    def one_shot(self) -> bool:
        return self.name in ONE_SHOT


def profile_list(profile: str) -> list[str]:
    """`v0`, `core` or a comma list such as `v0,observability`."""
    out = [x.strip() for x in profile.split(",") if x.strip()]
    if "core" in out and "s3-seaweedfs" not in out:
        out.append("s3-seaweedfs")
    return out


def load_services(p: Paths, profile: str) -> dict[str, Service]:
    env = env_values(p)
    doc = yaml.load(p.compose_file.read_text(), Loader=_Loader)
    services = dict(doc.get("services") or {})
    if "v0" in profile_list(profile):
        # The override files are part of the v0 stack (compose_cmd passes them too): their `profiles`, `image`,
        # `build` and `depends_on` win, and services defined only there are included (RENAME-D6).
        for f in (p.v0_file, p.compose_dir / "compose.v0.deps.yaml"):
            if not f.is_file():
                continue
            over = yaml.load(f.read_text(), Loader=_Loader) or {}
            for name, o in (over.get("services") or {}).items():
                base = dict(services.get(name) or {})
                for k in ("profiles", "image", "build"):
                    if k in o:
                        base[k] = o[k]
                if "depends_on" in o:
                    d = o["depends_on"]
                    base["depends_on"] = {**(base.get("depends_on") or {}), **d} if f.name.endswith("deps.yaml") \
                        and isinstance(d, dict) and isinstance(base.get("depends_on"), dict) else d
                services[name] = base
    out: dict[str, Service] = {}
    for name, svc in services.items():
        profiles = svc.get("profiles") or []
        if not set(profile_list(profile)) & set(profiles):
            continue
        image = interpolate(svc.get("image", ""), env)
        if image and "/" in image.split(":")[0] and not image.startswith(("localhost/", "docker.io/", "ghcr.io/", "quay.io/")):
            image = "docker.io/" + image
        elif image and "/" not in image.split(":")[0]:
            image = "docker.io/library/" + image
        dep = svc.get("depends_on") or {}
        deps = list(dep) if isinstance(dep, (dict, list)) else []
        s = Service(name, profiles, image, svc.get("build"), deps)
        if s.build:
            ctx = (p.compose_dir / interpolate(s.build.get("context", "."), env)).resolve()
            s.context = ctx
            s.dockerfile = (ctx / interpolate(s.build.get("dockerfile", "Dockerfile"), env)).resolve()
        out[name] = s
    return out


def image_exists(image: str) -> bool:
    return engine.exists("image", image)


def build_image(p: Paths, s: Service, env: dict[str, str], log) -> bool:  # noqa: ANN001
    assert s.build and s.dockerfile and s.context
    cmd = engine.cli("build", "-t", s.image, "-f", str(s.dockerfile))
    if s.build.get("target"):
        cmd += ["--target", s.build["target"]]
    args = s.build.get("args") or {}
    for k, v in args.items():
        cmd += ["--build-arg", f"{k}={interpolate(str(v), env)}"]
    cmd.append(str(s.context))
    log(f"build {s.name}: {s.image}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).splitlines()[-25:])
        log(f"  build FAILED for {s.name}:\n{tail}")
        return False
    return True


def build_plain(p: Paths, image: str, dockerfile: str, target: str | None, env: dict[str, str], log) -> bool:  # noqa: ANN001
    cmd = engine.cli("build", "-t", image, "-f", str(p.root / dockerfile))
    if target:
        cmd += ["--target", target]
    cmd += ["--build-arg", f"REPLICANT_GIT_SHA={env.get('REPLICANT_GIT_SHA', '000000000000')}", str(p.root)]
    log(f"build sandbox image {image}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  build FAILED for {image}:\n" + "\n".join((r.stdout + r.stderr).splitlines()[-25:]))
        return False
    return True


def compose_cmd(p: Paths, profile: str) -> list[str]:
    files = ["-f", str(p.compose_file)]
    profs = profile_list(profile)
    if "v0" in profs:
        files += ["-f", str(p.v0_file), "-f", str(p.compose_dir / "compose.v0.deps.yaml")]
    prof = [a for x in profs for a in ("--profile", x)]
    return [*engine.compose_bin(), "--project-name", PROJECT, *files, "--env-file", str(p.env_file), *prof]


def run_compose(p: Paths, profile: str, args: list[str], timeout: float | None = None,
                capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(compose_cmd(p, profile) + args, cwd=p.compose_dir, capture_output=capture, text=True,
                          timeout=timeout)


@dataclass
class CState:
    service: str
    name: str
    status: str            # created | running | exited | ...
    exit_code: int
    health: str            # healthy | unhealthy | starting | "" (no healthcheck)


def containers() -> dict[str, CState]:
    out: dict[str, CState] = {}
    for c in engine.inspect("container", engine.container_ids(f"label={engine.PROJECT_LABEL}={PROJECT}",
                                                              f"label={engine.SERVICE_LABEL}")):
        svc = ((c.get("Config") or {}).get("Labels") or {})[engine.SERVICE_LABEL]
        st = c.get("State") or {}
        health = ((st.get("Health") or st.get("Healthcheck") or {}).get("Status") or "")
        out[svc] = CState(svc, str(c["Name"]).lstrip("/"), st.get("Status", "?"), int(st.get("ExitCode") or 0), health)
    return out


def service_ok(s: Service, c: CState | None) -> tuple[bool, bool, str]:
    """(done, failed, description) for one service."""
    if c is None:
        return False, False, "not created"
    if s.one_shot:
        if c.status in ("exited", "stopped"):
            return (c.exit_code == 0), (c.exit_code != 0), f"exited {c.exit_code}"
        return False, False, c.status
    if c.status != "running":
        return False, c.status in ("exited", "stopped"), f"{c.status} (exit {c.exit_code})"
    if c.health in ("", "none"):
        return True, False, "running"
    return c.health == "healthy", False, f"running ({c.health})"


def wait_healthy(services: dict[str, Service], timeout_s: float, log) -> tuple[bool, dict[str, str]]:  # noqa: ANN001
    deadline = time.monotonic() + timeout_s
    last = ""
    while True:
        cs = containers()
        state = {n: service_ok(s, cs.get(n)) for n, s in services.items()}
        desc = {n: d for n, (_, _, d) in state.items()}
        failed = [n for n, (_, f, _) in state.items() if f]
        pending = [n for n, (ok, f, _) in state.items() if not ok and not f]
        line = f"waiting: {len(services) - len(pending) - len(failed)}/{len(services)} ready" + (
            f"; pending {', '.join(sorted(pending))}" if pending else "")
        if line != last:
            log(line)
            last = line
        if failed:
            return False, desc
        if not pending:
            return True, desc
        if time.monotonic() > deadline:
            return False, desc
        time.sleep(3)


def logs(container: str, tail: int | None = None, follow: bool = False) -> int:
    cmd = engine.cli("logs")
    if tail:
        cmd += ["--tail", str(tail)]
    if follow:
        cmd.append("--follow")
    return subprocess.run([*cmd, container]).returncode
