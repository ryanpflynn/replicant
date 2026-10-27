"""`replicant` CLI. Only the local `replicant dev` group exists in v0 (stack.md §4)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import typer

from replicant_cli import compose, prune as prunemod, secrets, seed as seedmod, testing
from replicant_cli.paths import paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Replicant CLI")
dev = typer.Typer(no_args_is_help=True, add_completion=False, help="Local stack (REPLICANT_ENV=local only)")
app.add_typer(dev, name="dev")


def log(msg: str) -> None:
    typer.echo(msg, err=False)


def _guard() -> None:
    env = os.environ.get("REPLICANT_ENV", "local")
    if env != "local":
        raise typer.BadParameter(f"`replicant dev` runs only with REPLICANT_ENV=local (got {env!r})")


def _closure(svcs: dict[str, compose.Service], wanted: list[str]) -> set[str]:
    out: set[str] = set()
    todo = [w for w in wanted if w in svcs]
    while todo:
        n = todo.pop()
        if n not in out:
            out.add(n)
            todo += [d for d in svcs[n].depends_on if d in svcs]
    return out


def _plan(profile: str, build: bool, only: list[str] | None = None) -> tuple[dict[str, compose.Service], dict[str, str]]:
    """Services to start, and the ones skipped (name -> reason). Builds replicant images first when asked."""
    p = paths()
    env = compose.env_values(p)
    svcs = compose.load_services(p, profile)
    if only:
        unknown = [o for o in only if o not in svcs]
        if unknown:
            raise typer.BadParameter(f"not in profile {profile}: {', '.join(unknown)}")
        keep = _closure(svcs, only)
        svcs = {n: s for n, s in svcs.items() if n in keep}
    skipped: dict[str, str] = {}
    built: set[str] = set()
    for s in svcs.values():
        if not s.build:
            continue
        if not (s.dockerfile and s.dockerfile.is_file()):
            skipped[s.name] = f"no {s.dockerfile.relative_to(p.root) if s.dockerfile else 'Dockerfile'} yet " \
                              f"({compose.OWNERS.get(s.name, '?')})"
            continue
        if build and s.image not in built:
            if not compose.build_image(p, s, env, log):
                skipped[s.name] = "image build failed"
                continue
            built.add(s.image)
        elif not compose.image_exists(s.image):
            skipped[s.name] = "image missing (run without --no-build)"
    if "runner-local" in svcs and "runner-local" not in skipped:
        for image, dockerfile, target in compose.SANDBOX_IMAGES:
            if not (p.root / dockerfile).is_file():
                skipped["runner-local"] = f"no {dockerfile} (sandbox image {image})"
                break
            if build:
                if not compose.build_plain(p, image, dockerfile, target, env, log):
                    skipped["runner-local"] = f"sandbox image {image} failed to build"
                    break
            elif not compose.image_exists(image):
                skipped["runner-local"] = f"sandbox image {image} missing (run without --no-build)"
                break
    for s in svcs.values():  # services that reuse another service's replicant image (run-grants)
        if not s.build and s.replicant_image and s.name not in skipped and not compose.image_exists(s.image):
            skipped[s.name] = f"image {s.image} not built ({compose.OWNERS.get(s.name, '?')})"
    changed = True
    while changed:  # drop dependents of skipped services, transitively
        changed = False
        for s in svcs.values():
            if s.name in skipped:
                continue
            bad = [d for d in s.depends_on if d in svcs and d in skipped]
            if bad:
                skipped[s.name] = f"depends on {', '.join(bad)}"
                changed = True
    return {n: s for n, s in svcs.items() if n not in skipped}, skipped


@dev.command()
def up(
    profile: str = typer.Option("v0", help="compose profile: v0 (M1) or core"),
    build: bool = typer.Option(True, "--build/--no-build", help="build replicant images first"),
    timeout: int = typer.Option(900, help="seconds to wait for health"),
    seed_after: bool = typer.Option(False, "--seed", help="run `replicant dev seed` when healthy"),
    services: str = typer.Option("", help="comma-separated subset (plus their dependencies)"),
    with_observability: bool = typer.Option(False, "--with-observability",
                                            help="add Alloy, Loki, Tempo, Prometheus, Grafana; turn on OTel export"),
    gateway: str = typer.Option("script", help="gateway backend: script (default, no key) or passthrough"),
) -> None:
    """Generate secrets, build images, start the stack and wait until healthy."""
    _guard()
    p = paths()
    if gateway not in ("script", "passthrough"):
        raise typer.BadParameter("--gateway must be script or passthrough")
    created = secrets.ensure(p)
    free = prunemod.free_gib()
    if free < prunemod.LOW_SPACE_GIB:
        log(f"WARNING: host free space is {free:.1f} GiB (< {prunemod.LOW_SPACE_GIB}); the Podman VM hangs when the "
            "disk fills. Run `replicant dev prune` (or --images/--volumes).")
    if gateway == "passthrough" and not secrets.import_provider_key(p):
        log("--gateway passthrough needs ANTHROPIC_API_KEY in replicant/dev/.env (not found or empty)")
        raise typer.Exit(2)
    secrets.set_env(p, {"REPLICANT_GATEWAY_MODE": gateway,
                        "REPLICANT_OTEL_DISABLED": "false" if with_observability else "true"})
    if with_observability and "observability" not in profile:
        profile = profile + ",observability"
    log(f"secrets: {len(created)} created under {p.dev_home.relative_to(p.root)}/ (values never printed)")
    start, skipped = _plan(profile, build, [x for x in services.split(',') if x] or None)
    for n, why in sorted(skipped.items()):
        log(f"skip {n}: {why}")
    if not start:
        raise typer.Exit(1)
    names = sorted(start)
    log(f"starting {len(names)} services (profile {profile}): {' '.join(names)}")
    t0 = time.monotonic()
    try:
        r = compose.run_compose(p, profile, ["up", "-d", "--no-build", *names], timeout=timeout)
    except subprocess.TimeoutExpired:
        log("podman-compose up timed out")
        raise typer.Exit(1)
    if r.returncode != 0:
        log("podman-compose up reported errors:\n" + "\n".join((r.stdout + r.stderr).splitlines()[-30:]))
    # podman-compose 1.6 removes exited dependents when it recreates a changed service but does not recreate them;
    # a second pass creates whatever is still missing.
    missing = [n for n in names if n not in compose.containers()]
    if missing:
        log(f"second pass for {', '.join(missing)}")
        compose.run_compose(p, profile, ["up", "-d", "--no-build", *missing], timeout=timeout)
    ok, desc = compose.wait_healthy(start, max(30, timeout - (time.monotonic() - t0)), log)
    for n in names:
        log(f"  {n:22s} {desc.get(n, '?')}")
    if not ok:
        bad = [n for n in names if not compose.service_ok(start[n], compose.containers().get(n))[0]]
        for n in bad:
            c = compose.containers().get(n)
            if c:
                log(f"--- logs {n} (tail 40)")
                compose.logs(c.name, tail=40)
        log(f"NOT healthy: {', '.join(bad)}")
        raise typer.Exit(1)
    log(f"healthy in {int(time.monotonic() - t0)} s")
    log("URLs: temporal UI http://127.0.0.1:8233  gitea http://127.0.0.1:3300  s3 http://127.0.0.1:8333  "
        "api http://127.0.0.1:8401" + ("  grafana http://127.0.0.1:3001  loki :3100  tempo :3200"
                                        if "observability" in profile else ""))
    log(f"gateway backend: {gateway}")
    if seed_after and "control-plane-api" in start:
        raise typer.Exit(seedmod.run(p, *seedmod.default_urls(), None, False, False, log))


@dev.command()
def down(volumes: bool = typer.Option(False, "--volumes", "-v", help="also delete all state volumes")) -> None:
    """Stop and remove every container of project `replicant` (and its volumes with --volumes)."""
    _guard()
    p = paths()
    for prof in ("v0,observability", "core,observability"):
        try:
            compose.run_compose(p, prof, ["down", *(["-v"] if volumes else [])], timeout=300)
        except subprocess.TimeoutExpired:
            pass
    left = compose.containers()
    if left:
        subprocess.run(["podman", "rm", "-f", *[c.name for c in left.values()]], capture_output=True)
    if volumes:  # stack.md §7: sandbox containers and volumes labelled with a run id go with the state
        sel = "label=replicant.example.com/run-id"
        for _ in range(3):  # pause containers go last: the rest of each pod group shares their netns
            r = subprocess.run(["podman", "ps", "-a", "-q", "--filter", sel], capture_output=True, text=True)
            if not r.stdout.split():
                break
            subprocess.run(["podman", "rm", "-f", "--depend", *r.stdout.split()], capture_output=True)
        r = subprocess.run(["podman", "volume", "ls", "-q", "--filter", sel], capture_output=True, text=True)
        if r.stdout.split():
            subprocess.run(["podman", "volume", "rm", "-f", *r.stdout.split()], capture_output=True)
        left_vols: list[str] = []
        for _ in range(5):  # a volume still held by a stopping container is retried, then reported
            r = subprocess.run(["podman", "volume", "ls", "-q", "--filter",
                                f"label=io.podman.compose.project={compose.PROJECT}"], capture_output=True, text=True)
            left_vols = [v for v in r.stdout.split()] + [
                v for v in subprocess.run(["podman", "volume", "ls", "-q"], capture_output=True, text=True).stdout.split()
                if v.startswith(f"{compose.PROJECT}_")]
            left_vols = sorted(set(left_vols))
            if not left_vols:
                break
            left = compose.containers()
            if left:
                subprocess.run(["podman", "rm", "-f", *[c.name for c in left.values()]], capture_output=True)
            subprocess.run(["podman", "volume", "rm", "-f", *left_vols], capture_output=True)
            time.sleep(2)
        if left_vols:
            log(f"WARNING: volumes not removed: {' '.join(left_vols)}")
            raise typer.Exit(1)
    for net in ("replicant-core", "replicant-sbx", "replicant-egress"):
        subprocess.run(["podman", "network", "rm", net], capture_output=True)
    log("stack down" + (" (volumes removed)" if volumes else ""))


@dev.command()
def status(profile: str = typer.Option("v0,observability")) -> None:
    """Per-service state and health, migration revision, Temporal namespace."""
    _guard()
    p = paths()
    svcs = compose.load_services(p, profile)
    cs = compose.containers()
    log(f"{'SERVICE':22s} {'OWNER':9s} STATE")
    for n in sorted(svcs):
        s = svcs[n]
        c = cs.get(n)
        if s.build and not (s.dockerfile and s.dockerfile.is_file()):
            st = "not built yet (no Dockerfile)"
        else:
            st = compose.service_ok(s, c)[2]
        log(f"{n:22s} {compose.OWNERS.get(n, ''):9s} {st}")
    mig = cs.get("migrate")
    if mig:
        out = subprocess.run(["podman", "logs", mig.name], capture_output=True, text=True)
        m = re.findall(r'"event": "migrate\.succeeded".*?"to": "([^"]+)"', out.stdout + out.stderr)
        log(f"migration revision: {m[-1] if m else 'unknown (see replicant dev logs migrate)'}")
    tmp = cs.get("temporal")
    if tmp and tmp.status == "running":
        r = subprocess.run(["podman", "exec", tmp.name, "temporal", "operator", "namespace", "describe", "-n",
                            "replicant-local", "--address", "127.0.0.1:7233"], capture_output=True, text=True)
        log("temporal namespace replicant-local: " + ("registered" if r.returncode == 0 else "missing"))


@dev.command()
def logs(
    services: list[str] = typer.Argument(None, help="services (default: all)"),
    follow: bool = typer.Option(False, "--follow", "-f"),
    tail: int = typer.Option(200, help="lines per service"),
) -> None:
    """Container logs of the stack (podman logs)."""
    _guard()
    cs = compose.containers()
    names = services or sorted(cs)
    if follow and len(names) == 1:
        raise typer.Exit(compose.logs(cs[names[0]].name, tail=tail, follow=True))
    for n in names:
        c = cs.get(n)
        if not c:
            log(f"--- {n}: no container")
            continue
        log(f"--- {n} ({c.name})")
        compose.logs(c.name, tail=tail)


@dev.command()
def seed(
    only: str = typer.Option(None, help="run one step (e.g. 02-tenants or tenants)"),
    force: bool = typer.Option(False, help="re-run steps already recorded as done"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Seed tenants, repo binding, platform caps, the bundle bootstrap; check the Gitea webhook (APIs only)."""
    _guard()
    raise typer.Exit(seedmod.run(paths(), *seedmod.default_urls(), only, force, dry_run, log))


@dev.command()
def test(
    suite: str = typer.Argument("unit", help="unit | e2e | m3 | all (unit then e2e)"),
    only: str = typer.Option("", help="comma-separated suites (e.g. control-plane-api,go,web:vitest,e2e01)"),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="stop starting suites after the first failure"),
    junit: str = typer.Option(None, "--junit", help="directory for one JUnit XML file per suite"),
    slow: bool = typer.Option(False, "--slow", help="unit: all 44 worker scenarios (default: the fast five)"),
    keep_db: bool = typer.Option(False, "--keep-db", help="unit: leave the smoke Postgres (ctl-pg) running"),
    db: bool = typer.Option(True, "--db/--no-db", help="unit: run the DB-gated tests (smoke Postgres)"),
    build: bool = typer.Option(False, "--build", help="unit: rebuild the migrate image first; m3: build images"),
    jobs: int = typer.Option(4, "--jobs", "-j", help="unit: suites run in parallel"),
    yes: bool = typer.Option(False, "--yes", help="m3: confirm the destructive run (down -v)"),
) -> None:
    """Run the test suites: unit (every package, Go, web), e2e (live stack), m3 (clean-stack pass)."""
    _guard()
    p = paths()
    sel = [x.strip() for x in only.split(",") if x.strip()]
    if suite not in ("unit", "e2e", "m3", "all"):
        raise typer.BadParameter("suite must be unit, e2e, m3 or all")
    if suite == "m3":
        if not yes:
            log("m3 tears the stack down with volumes (down -v) and takes 15-20 min; pass --yes to run it")
            raise typer.Exit(2)
        raise typer.Exit(testing.m3(p, build=build, log=log))
    rc = 0
    if suite in ("unit", "all"):
        rc = testing.unit(p, only=sel if suite == "unit" else [], fail_fast=fail_fast, junit=junit, slow=slow,
                          keep_db=keep_db, db=db, build=build, jobs=jobs, log=log)
        if rc and fail_fast:
            raise typer.Exit(rc)
    if suite in ("e2e", "all"):
        rc = testing.e2e(p, only=sel if suite == "e2e" else [], fail_fast=fail_fast, junit=junit, log=log) or rc
    raise typer.Exit(rc)


@dev.command()
def prune(
    images: bool = typer.Option(False, "--images", help="also unused images (keeps replicant :dev, compose, test images)"),
    volumes: bool = typer.Option(False, "--volumes", help="also anonymous unused volumes and finished sandbox-run volumes"),
    yes: bool = typer.Option(False, "--yes", help="do not prompt before --images / --volumes removals"),
) -> None:
    """Free disk in the Podman VM: finished sandbox containers, exited one-shots, dangling images, fstrim."""
    _guard()
    raise typer.Exit(prunemod.run(paths(), images=images, volumes=volumes, yes=yes,
                                  confirm=lambda q: typer.confirm(q, default=False), log=log))


def main() -> None:  # pragma: no cover
    sys.exit(app())


if __name__ == "__main__":  # pragma: no cover
    main()
