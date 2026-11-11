"""`replicant dev test {unit,e2e,m3}`: one command for every test suite in the repo.

unit  Python packages (pytest, DB-gated tests against the control-plane-api smoke Postgres), Go modules in
      docker.io/library/golang:1.26, web typecheck + vitest in docker.io/library/node:22. Suites run in parallel;
      one table at the end, non-zero exit on any failure.
e2e   the system E2E drivers (tests/e2e) and the Playwright live specs, against the running stack.
m3    tests/run_m3_pass.sh (clean stack: down -v, E2E-01, SEC-EGRESS, chaos). Destructive; needs --yes.

Logs and JUnit XML of the last run land in .test-results/ (gitignored); --junit DIR copies the XML there too.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from replicant_cli import compose, engine
from replicant_cli.paths import Paths

GO_IMAGE = "docker.io/library/golang:1.26"
NODE_IMAGE = "docker.io/library/node:22"
PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright:v1.64.1-noble"
GO_CACHE_VOLUMES = ("plat-gomod", "plat-gocache")  # reused across runs (also used by the runner-contract driver)
PY_ROOTS = ("services", "workers", "libs/python", "cli", "tools/slack-mock")
DB_SUITES = {"control-plane-api", "github-integration", "memory-service"}  # share the smoke Postgres: run one at a time
WORKER = "replicant-worker"
SMOKE_PG_PORT = 55432
E2E_DRIVERS = ("e2e01", "e2e04", "e2e02", "e2e03", "e2e05", "e2e06", "e2e07", "e2e08")  # e2e04 reads the job e2e01 just finished
E2E_FILES = {"e2e01": "e2e01_v0.py", "e2e02": "e2e02_notify.py", "e2e03": "e2e03_slack.py",
             "e2e04": "e2e04_graph.py", "e2e05": "e2e05_config.py",
             "e2e06": "e2e06_memory.py", "e2e07": "e2e07_pr_feedback.py",
             "e2e08": "e2e08_onboarding.py"}
PLAYWRIGHT_SPECS = ("e2e/01-screens.spec.ts", "e2e/02-live.spec.ts", "e2e/03-after.spec.ts",
                    "e2e/05-waiting.spec.ts", "e2e/07-simple-view.spec.ts", "e2e/08-project-settings.spec.ts",
                    "e2e/09-memory.spec.ts", "e2e/10-onboarding.spec.ts")


@dataclass
class Result:
    name: str
    status: str = "NOT RUN"     # PASS | FAIL | SKIP (all tests skipped) | NOT RUN
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    seconds: float = 0.0
    log: Path | None = None
    note: str = ""


@dataclass
class Suite:
    name: str
    run: Callable[["Ctx", "Suite"], list[Result]]
    cwd: Path | None = None
    package: str = ""
    group: str = ""              # suites with the same group never run at the same time
    rows: list[str] = field(default_factory=list)  # result names this suite produces (default: its own name)


@dataclass
class Ctx:
    p: Paths
    out: Path
    env: dict[str, str]
    slow: bool = False
    log: Callable[[str], None] = print


# ---------------------------------------------------------------------------------------------------------- helpers
def _junit_counts(xml: Path) -> tuple[int, int, int]:
    """(passed, failed, skipped) from a JUnit XML file (pytest, vitest or ours)."""
    root = ET.parse(xml).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    tests = fail = skip = 0
    for s in suites:
        tests += int(s.get("tests", 0))
        fail += int(s.get("failures", 0)) + int(s.get("errors", 0))
        skip += int(s.get("skipped", 0))
    return tests - fail - skip, fail, skip


def _write_junit(path: Path, suite: str, cases: list[tuple[str, str, float, str]]) -> None:
    """cases: (name, pass|fail|skip, seconds, output)."""
    ts = ET.Element("testsuite", name=suite, tests=str(len(cases)),
                    failures=str(sum(c[1] == "fail" for c in cases)), errors="0",
                    skipped=str(sum(c[1] == "skip" for c in cases)), time=f"{sum(c[2] for c in cases):.3f}")
    for name, st, secs, out in cases:
        cls, _, short = name.rpartition(".")
        tc = ET.SubElement(ts, "testcase", classname=cls or suite, name=short or name, time=f"{secs:.3f}")
        if st == "fail":
            ET.SubElement(tc, "failure", message="failed").text = out[-4000:]
        elif st == "skip":
            ET.SubElement(tc, "skipped")
    ET.ElementTree(ts).write(path, encoding="utf-8", xml_declaration=True)


def _status(r: Result, rc: int) -> Result:
    if r.failed or rc not in (0, 5):  # pytest 5 = nothing collected
        r.status = "FAIL"
        if not r.failed:
            r.failed = 1
            r.note = r.note or f"exit {rc}"
    elif r.passed == 0 and r.skipped:
        r.status = "SKIP"
    else:
        r.status = "PASS"
    return r


def _run_logged(cmd: list[str], log: Path, cwd: Path | None = None, env: dict[str, str] | None = None,
                timeout: float | None = None) -> int:
    """Run cmd with output to log. On timeout the process gets SIGTERM first (pytest then tears down its fixtures,
    e.g. the worker scenarios' Temporal dev server) and SIGKILL 30 s later."""
    with log.open("w") as f:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            f.write(f"\n*** timed out after {timeout}s\n")
            return 124
        except BaseException:
            proc.terminate()
            proc.wait()
            raise


# ---------------------------------------------------------------------------------------------------------- python
def python_packages(p: Paths) -> list[tuple[str, str, Path]]:
    """(suite name, package name, dir) for every workspace member under PY_ROOTS that has tests/."""
    out = []
    for root in PY_ROOTS:
        base = p.root / root
        cands = [base] if (base / "pyproject.toml").is_file() else sorted(d for d in base.iterdir() if d.is_dir())
        for d in cands:
            if (d / "pyproject.toml").is_file() and (d / "tests").is_dir():
                name = tomllib.loads((d / "pyproject.toml").read_text())["project"]["name"]
                out.append((d.name, name, d))
    return out


# Worker scenarios gated on a build-time variable (replicant_worker/build_config.py) that would change every other
# scenario: with --slow they run in their own pytest invocation with that variable set.
WORKER_ENV_GROUPS: tuple[tuple[tuple[str, ...], dict[str, str]], ...] = (
    (("license_prestep",), {"REPLICANT_EXTRA_NODE_MODULES": "replicant_worker.nodes.license_scan"}),
    (("mid_run_can",), {"REPLICANT_HISTORY_CAN_EVENTS": "150"}),
    (("dependency_outage", "dependency_giveup", "identity_outage"), {"REPLICANT_TEST_FAST_RETRY": "1"}),
)


def _scenario_k(names: tuple[str, ...]) -> str:
    return "test_scenario and (" + " or ".join(names) + ")"


def _run_pytest(ctx: Ctx, s: Suite) -> list[Result]:
    r = Result(s.name, log=ctx.out / f"{s.name}.log")
    base = ["uv", "run", "--no-sync", "--package", s.package, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-o", "junit_family=xunit2"]
    runs: list[tuple[list[str], dict[str, str]]] = [([], {})]
    if s.name == WORKER and ctx.slow:
        gated = tuple(n for names, _ in WORKER_ENV_GROUPS for n in names)
        runs = [(["-m", "slow or not slow", "-k", f"not ({_scenario_k(gated)})"], {})]
        runs += [(["-m", "slow or not slow", "-k", _scenario_k(names), "tests/test_scenarios.py"], extra)
                 for names, extra in WORKER_ENV_GROUPS]
    t0 = time.monotonic()
    rc = 0
    for i, (args, extra) in enumerate(runs):
        sfx = f"-{i}" if i else ""
        xml = ctx.out / f"junit-{s.name}{sfx}.xml"
        log = r.log if not i else ctx.out / f"{s.name}{sfx}.log"
        if extra:
            ctx.log(f"     {s.name}: {' '.join(f'{k}={v}' for k, v in extra.items())} -k '{args[3]}'")
        one = _run_logged([*base, f"--junitxml={xml}", *args], log, cwd=s.cwd, env={**ctx.env, **extra}, timeout=7200)
        rc = rc or (0 if one in (0, 5) else one)
        if xml.is_file():
            p, f, sk = _junit_counts(xml)
            r.passed, r.failed, r.skipped = r.passed + p, r.failed + f, r.skipped + sk
        if i:
            with r.log.open("a") as main_log:  # one log per suite: append the gated runs to the main one
                main_log.write(f"\n===== {' '.join(f'{k}={v}' for k, v in extra.items())} =====\n" + log.read_text())
    r.seconds = time.monotonic() - t0
    return [_status(r, rc)]


# ---------------------------------------------------------------------------------------------------------- go
def go_modules(p: Paths) -> list[str]:
    txt = (p.root / "go.work").read_text()
    block = re.search(r"use\s*\((.*?)\)", txt, re.S)
    mods = re.findall(r"^\s*(\./\S+)", block.group(1), re.M) if block else re.findall(r"^use\s+(\./\S+)", txt, re.M)
    return [m[2:] for m in mods]


def _go_row(mod: str) -> str:
    return "go:" + mod.rsplit("/", 1)[-1]


def _run_go(ctx: Ctx, s: Suite) -> list[Result]:
    mods = go_modules(ctx.p)
    log = ctx.out / "go.log"
    script = ('for m in ' + " ".join(mods) + '; do s=$(date +%s.%N); (cd "$m" && go test -json ./... 2>&1); '
              'rc=$?; echo "@@END $m $rc $s $(date +%s.%N)"; done')
    vols = []
    for v, mnt in zip(GO_CACHE_VOLUMES, ("/go/pkg/mod", "/root/.cache")):
        vols += ["-v", f"{v}:{mnt}"]
    cmd = [*engine.cli("run"), "--rm", "-v", f"{ctx.p.root}:/src", "-w", "/src", *vols, "-e", "GOTOOLCHAIN=local",
           "-e", "GOFLAGS=-buildvcs=false", GO_IMAGE, "sh", "-c", script]
    rc = _run_logged(cmd, log, timeout=1800)
    events: dict[str, list[dict]] = {m: [] for m in mods}
    ends: dict[str, tuple[int, float]] = {}
    cur = 0
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith("@@END "):
            _, m, mrc, a, b = line.split()
            ends[m] = (int(mrc), float(b) - float(a))
            cur += 1
            continue
        if line.startswith("{") and cur < len(mods):
            try:
                events[mods[cur]].append(json.loads(line))
            except json.JSONDecodeError:
                pass
    out = []
    for m in mods:
        r = Result(_go_row(m), log=log)
        cases: dict[tuple[str, str], tuple[str, float, list[str]]] = {}
        pkg_fail = []
        for ev in events[m]:
            act, test = ev.get("Action"), ev.get("Test")
            key = (ev.get("Package", ""), test) if test else None
            if key and act == "output":
                st, secs, outl = cases.get(key, ("run", 0.0, []))
                outl.append(ev.get("Output", ""))
                cases[key] = (st, secs, outl)
            elif key and act in ("pass", "fail", "skip"):
                cases[key] = (act, float(ev.get("Elapsed") or 0), cases.get(key, ("", 0, []))[2])
            elif not key and act == "fail":
                pkg_fail.append(ev.get("Package", m))
        top = {k: v for k, v in cases.items() if "/" not in k[1]}  # count top-level tests only
        r.passed = sum(v[0] == "pass" for v in top.values())
        r.failed = sum(v[0] == "fail" for v in top.values())
        r.skipped = sum(v[0] == "skip" for v in top.values())
        mrc, r.seconds = ends.get(m, (rc or 1, 0.0))
        if pkg_fail and not r.failed:
            r.note = "build/package failure: " + ", ".join(sorted(set(pkg_fail)))[:80]
        junit = [(f"{k[0]}.{k[1]}", v[0], v[1], "".join(v[2])) for k, v in top.items()]
        junit += [(f"{pk}.[package]", "fail", 0.0, "package failed (see go.log)") for pk in sorted(set(pkg_fail))]
        _write_junit(ctx.out / f"junit-{r.name.replace(':', '-')}.xml", r.name, junit)
        out.append(_status(r, mrc))
    return out


# ---------------------------------------------------------------------------------------------------------- lint
def _lint_result(ctx: Ctx, name: str, cases: list[tuple[str, str, float, str]], log: Path, rc: int) -> Result:
    r = Result(name, log=log, seconds=sum(c[2] for c in cases))
    r.passed = sum(c[1] == "pass" for c in cases)
    r.failed = sum(c[1] == "fail" for c in cases)
    _write_junit(ctx.out / f"junit-{name.replace(':', '-')}.xml", name, cases)
    return _status(r, 0 if not r.failed and rc == 0 else (rc or 1))


def _run_lint_python(ctx: Ctx, s: Suite) -> list[Result]:
    """ruff check with the root pyproject's lenient config (pyflakes, import order, a bugbear subset)."""
    log = ctx.out / "lint-python.log"
    t0 = time.monotonic()
    rc = _run_logged(["uv", "run", "--no-sync", "ruff", "check", "--no-cache", "."], log, cwd=ctx.p.root, env=ctx.env,
                     timeout=600)
    return [_lint_result(ctx, s.name, [("ruff.check", "pass" if rc == 0 else "fail", time.monotonic() - t0,
                                        log.read_text(errors="replace"))], log, rc)]


def _run_lint_go(ctx: Ctx, s: Suite) -> list[Result]:
    """go vet ./... in every go.work module (vet writes no binaries into the module directories)."""
    mods = go_modules(ctx.p)
    log = ctx.out / "lint-go.log"
    script = ('for m in ' + " ".join(mods) + '; do s=$(date +%s.%N); echo "@@BEGIN $m"; (cd "$m" && go vet ./... 2>&1); '
              'echo "@@END $m $? $s $(date +%s.%N)"; done')
    vols = []
    for v, mnt in zip(GO_CACHE_VOLUMES, ("/go/pkg/mod", "/root/.cache"), strict=True):
        vols += ["-v", f"{v}:{mnt}"]
    rc = _run_logged([*engine.cli("run"), "--rm", "-v", f"{ctx.p.root}:/src", "-w", "/src", *vols, "-e", "GOTOOLCHAIN=local",
                      "-e", "GOFLAGS=-buildvcs=false", GO_IMAGE, "sh", "-c", script], log, timeout=1200)
    cases, out, cur = [], [], None
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith("@@BEGIN "):
            cur, out = line.split()[1], []
        elif line.startswith("@@END "):
            _, m, mrc, a, b = line.split()
            cases.append((f"go vet {m}", "pass" if mrc == "0" else "fail", float(b) - float(a), "\n".join(out)))
            cur = None
        elif cur:
            out.append(line)
    if len(cases) != len(mods):
        cases.append(("go vet [container]", "fail", 0.0, f"container exit {rc}; see {log}"))
    return [_lint_result(ctx, s.name, cases, log, 0 if len(cases) == len(mods) else rc or 1)]


# ---------------------------------------------------------------------------------------------------------- web
def _run_web(ctx: Ctx, s: Suite) -> list[Result]:
    web = ctx.p.root / "web" / "control-plane-web"
    wout = ctx.out / "web"
    wout.mkdir(exist_ok=True)
    install = "" if (web / "node_modules" / ".bin" / "vitest").exists() else \
        "npm i -g pnpm@12.15.0 >/out/install.log 2>&1 && pnpm install --frozen-lockfile >>/out/install.log 2>&1 && "
    script = (install + 's=$(date +%s.%N); node_modules/.bin/tsc --noEmit >/out/tsc.log 2>&1; '
              'echo "$? $s $(date +%s.%N)" >/out/tsc.rc; s=$(date +%s.%N); '
              'node_modules/.bin/vitest run --reporter=default --reporter=junit --outputFile.junit=/out/vitest.xml '
              '>/out/vitest.log 2>&1; echo "$? $s $(date +%s.%N)" >/out/vitest.rc')
    log = ctx.out / "web.log"
    rc = _run_logged([*engine.cli("run"), "--rm", "-v", f"{web}:/app", "-v", f"{wout}:/out", "-w", "/app", NODE_IMAGE,
                      "sh", "-c", script], log, timeout=1200)
    out = []
    for name, f in (("web:tsc", "tsc"), ("web:vitest", "vitest")):
        r = Result(name, log=wout / f"{f}.log")
        rcf = wout / f"{f}.rc"
        if not rcf.is_file():
            r.note = f"did not run (container exit {rc}, see {log})"
            out.append(_status(r, rc or 1))
            continue
        frc, a, b = rcf.read_text().split()
        r.seconds = float(b) - float(a)
        if f == "vitest" and (wout / "vitest.xml").is_file():
            shutil.copy(wout / "vitest.xml", ctx.out / "junit-web-vitest.xml")
            r.passed, r.failed, r.skipped = _junit_counts(wout / "vitest.xml")
        elif f == "tsc":
            ok = frc == "0"
            r.passed, r.failed = int(ok), int(not ok)
            _write_junit(ctx.out / "junit-web-tsc.xml", "web:tsc",
                         [("tsc.noEmit", "pass" if ok else "fail", r.seconds, r.log.read_text(errors="replace"))])
        out.append(_status(r, int(frc)))
    return out


# ---------------------------------------------------------------------------------------------------------- DB env
MIGRATE_SOURCES = ("replicant_db", "bootstrap", "tools", "Dockerfile.migrate", "pyproject.toml")


def migrate_image_stale(p: Paths) -> str | None:
    """Why localhost/replicant/migrate:dev is older than replicant-db's sources (None when current)."""
    built = engine.image_created("localhost/replicant/migrate:dev")
    if built is None:
        return "image missing"
    base = p.root / "libs" / "python" / "replicant-db"
    newest, which = 0.0, ""
    for src in MIGRATE_SOURCES:
        q = base / src
        files = [q] if q.is_file() else [f for f in q.rglob("*") if f.is_file() and "__pycache__" not in f.parts]
        for f in files:
            if f.stat().st_mtime > newest:
                newest, which = f.stat().st_mtime, str(f.relative_to(p.root))
    return f"{which} changed after the image was built" if newest > built else None


def build_migrate(p: Paths, log: Callable[[str], None]) -> bool:
    svc = compose.load_services(p, "v0")["migrate"]
    ok = compose.build_image(p, svc, compose.env_values(p), log)
    subprocess.run(engine.cli("image", "prune", "-f"), capture_output=True)  # dangling layers of the old image
    return ok


class SmokeDb:
    """control-plane-api's tests/smoke/env.sh for the session. A Postgres someone else already started (ctl-pg) is
    reused and left running; one this run started is torn down unless keep."""

    def __init__(self, p: Paths, keep: bool, log: Callable[[str], None]) -> None:
        self.p, self.keep, self.log = p, keep, log
        self.script = p.root / "services" / "control-plane-api" / "tests" / "smoke" / "env.sh"
        self.home = Path(os.environ.get("CTL_HOME") or Path(os.environ.get("TMPDIR", "/tmp")) / "ctl-smoke")
        self.started = False

    def up(self) -> dict[str, str]:
        pre = engine.exists("container", "ctl-pg")
        env = {**os.environ, "CTL_HOME": str(self.home)}
        t0 = time.monotonic()
        r = subprocess.run(["sh", str(self.script), "up"], env=env, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("smoke DB (env.sh up) failed:\n" + "\n".join((r.stdout + r.stderr).splitlines()[-25:]))
        self.started = not pre
        self.log(f"smoke DB {'reused' if pre else 'started'} on 127.0.0.1:{SMOKE_PG_PORT} "
                 f"(migrated in {time.monotonic() - t0:.0f}s)")
        sec = self.home / "secrets"
        su_pw = (sec / "pg_replicant_superuser").read_text().strip()  # token_urlsafe: no URL escaping needed
        host = f"127.0.0.1:{SMOKE_PG_PORT}/replicant"
        return {"CTL_HOME": str(self.home),
                "CTL_DB_DSN": f"postgresql+psycopg://svc_control_plane_api@{host}",
                "CTL_DB_PASSWORD_FILE": str(sec / "db_svc_control_plane_api"),
                "CTL_SU_DSN": f"postgresql+psycopg://postgres:{su_pw}@{host}",
                "CTL_GI_DB_DSN": f"postgresql+psycopg://svc_github_integration@{host}",
                "CTL_GI_DB_PASSWORD_FILE": str(sec / "db_svc_github_integration"),
                "CTL_MEM_DB_DSN": f"postgresql+psycopg://svc_memory_service@{host}",
                "CTL_MEM_DB_PASSWORD_FILE": str(sec / "db_svc_memory_service")}

    def down(self) -> None:
        if self.started and not self.keep:
            subprocess.run(["sh", str(self.script), "down"], capture_output=True,
                           env={**os.environ, "CTL_HOME": str(self.home)})
            self.log("smoke DB torn down")
        elif self.started:
            self.log(f"smoke DB kept (--keep-db); stop it with: sh {self.script.relative_to(self.p.root)} down")


# ---------------------------------------------------------------------------------------------------------- unit
def unit_suites(p: Paths) -> list[Suite]:
    suites = [Suite(n, _run_pytest, cwd=d, package=pkg, group="db" if n in DB_SUITES else "")
              for n, pkg, d in python_packages(p)]
    suites.sort(key=lambda s: s.name != WORKER)  # the long pole starts first
    suites.append(Suite("go", _run_go, rows=[_go_row(m) for m in go_modules(p)]))
    suites.append(Suite("lint:python", _run_lint_python))
    suites.append(Suite("lint:go", _run_lint_go))
    suites.append(Suite("web", _run_web, rows=["web:tsc", "web:vitest"]))
    return suites


def _selected(suites: list[Suite], only: list[str]) -> list[Suite]:
    if not only:
        return suites
    out = []
    for s in suites:
        rows = s.rows or [s.name]
        if s.name in only or any(r in only for r in rows):
            out.append(s)
    unknown = [o for o in only if not any(o == s.name or o in (s.rows or []) for s in suites)]
    if unknown:
        names = sorted({n for s in suites for n in [s.name, *s.rows]})
        raise SystemExit(f"unknown suite(s): {', '.join(unknown)}; known: {', '.join(names)}")
    return out


def run_suites(ctx: Ctx, suites: list[Suite], jobs: int, fail_fast: bool) -> list[Result]:
    locks: dict[str, threading.Lock] = {s.group: threading.Lock() for s in suites if s.group}
    stop = threading.Event()
    results: dict[str, list[Result]] = {}

    def one(s: Suite) -> list[Result]:
        lock = locks.get(s.group)
        if lock:
            lock.acquire()
        try:
            if stop.is_set():
                return [Result(n, note="skipped by --fail-fast") for n in (s.rows or [s.name])]
            try:
                res = s.run(ctx, s)
            except Exception as e:  # noqa: BLE001  (a broken runner is a failed suite, not a crash)
                res = [Result(n, status="FAIL", failed=1, note=f"runner error: {e}"[:120]) for n in (s.rows or [s.name])]
        finally:
            if lock:
                lock.release()
        for r in res:
            extra = f"  ({r.note})" if r.note else ""
            ctx.log(f"{r.status:4s} {r.name:28s} {r.passed:4d} passed {r.failed:3d} failed {r.skipped:3d} skipped "
                    f"{r.seconds:6.1f}s{extra}")
            if r.status == "FAIL" and r.log and r.log.is_file():
                tail = r.log.read_text(errors="replace").splitlines()[-30:]
                ctx.log("     | " + "\n     | ".join(tail))
                ctx.log(f"     full log: {r.log}")
        if fail_fast and any(r.status == "FAIL" for r in res):
            stop.set()
        return res

    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(one, s): s for s in suites}
        for f in cf.as_completed(futs):
            results[futs[f].name] = f.result()
    return [r for s in suites for r in results.get(s.name, [])]


def print_table(results: list[Result], wall: float, log: Callable[[str], None]) -> None:
    w = max([len(r.name) for r in results] + [5])
    log("")
    log(f"{'SUITE':{w}s}  {'RESULT':7s} {'PASS':>5s} {'FAIL':>5s} {'SKIP':>5s} {'TIME':>8s}")
    log("-" * (w + 36))
    for r in results:
        log(f"{r.name:{w}s}  {r.status:7s} {r.passed:5d} {r.failed:5d} {r.skipped:5d} {r.seconds:7.1f}s"
            + (f"  {r.note}" if r.note else ""))
    log("-" * (w + 36))
    tot = Result("TOTAL", passed=sum(r.passed for r in results), failed=sum(r.failed for r in results),
                 skipped=sum(r.skipped for r in results))
    bad = [r.name for r in results if r.status in ("FAIL", "NOT RUN")]
    log(f"{'TOTAL':{w}s}  {'FAIL' if bad else 'PASS':7s} {tot.passed:5d} {tot.failed:5d} {tot.skipped:5d} "
        f"{wall:7.1f}s wall")
    if bad:
        log(f"failed or not run: {', '.join(bad)}")


def out_dir(p: Paths) -> Path:
    d = p.root / ".test-results"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir()
    return d


def copy_junit(out: Path, junit: str | None, log: Callable[[str], None]) -> None:
    if not junit:
        return
    dst = Path(junit)
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in out.glob("junit-*.xml"):
        shutil.copy(f, dst / f.name)
        n += 1
    log(f"JUnit XML: {n} file(s) in {dst}")


def unit(p: Paths, *, only: list[str], fail_fast: bool, junit: str | None, slow: bool, keep_db: bool, db: bool,
         build: bool, jobs: int, log: Callable[[str], None]) -> int:
    t0 = time.monotonic()
    suites = _selected(unit_suites(p), only)
    out = out_dir(p)
    r = subprocess.run(["uv", "sync", "--all-packages", "--frozen", "--quiet"], cwd=p.root, capture_output=True,
                       text=True)
    if r.returncode != 0:
        log("uv sync --all-packages failed:\n" + r.stderr[-2000:])
        return 1
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTL_")}
    if slow:
        log("--slow: all worker scenarios (about 25 min)")
    smoke = None
    if db and any(s.name in DB_SUITES for s in suites):
        why = migrate_image_stale(p)
        if build or why:
            log(f"building localhost/replicant/migrate:dev ({'--build' if build else why})")
            if not build_migrate(p, log):
                return 1
        smoke = SmokeDb(p, keep_db, log)
        try:
            env.update(smoke.up())
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log(f"{e}\n(--no-db runs the DB-gated tests as skipped)")
            return 1
    elif not db:
        log("--no-db: DB-gated tests report SKIP")
    log(f"running {len(suites)} suite(s), {jobs} at a time; logs in {out}")
    ctx = Ctx(p, out, env, slow=slow, log=log)
    try:
        results = run_suites(ctx, suites, jobs, fail_fast)
    finally:
        if smoke:
            smoke.down()
    print_table(results, time.monotonic() - t0, log)
    copy_junit(out, junit, log)
    return 1 if any(r.status in ("FAIL", "NOT RUN") for r in results) else 0


# ---------------------------------------------------------------------------------------------------------- e2e
def _run_driver(ctx: Ctx, s: Suite) -> list[Result]:
    r = Result(s.name, log=ctx.out / f"{s.name}.log")
    xml = ctx.out / f"junit-{s.name}.xml"
    t0 = time.monotonic()
    rc = _run_logged(["uv", "run", "--no-sync", "--package", "replicant-e2e", "python", "-m", "pytest", "-q", "-s",
                      "-p", "no:cacheprovider", f"--junitxml={xml}", E2E_FILES[s.name]], r.log, cwd=s.cwd,
                     env=ctx.env, timeout=3600)
    r.seconds = time.monotonic() - t0
    if xml.is_file():
        r.passed, r.failed, r.skipped = _junit_counts(xml)
    return [_status(r, rc)]


def _run_playwright(ctx: Ctx, s: Suite) -> list[Result]:
    """live_gate.py make (a real publish gate) -> specs 01-03 (screens, approve in the UI, after) plus the specs that
    need no prepared state -> live_gate.py finish (merge, succeeded)."""
    r = Result(s.name, log=ctx.out / "playwright.log")
    web = ctx.p.root / "web" / "control-plane-web"
    gate_py = ["uv", "run", "--no-sync", "--package", "replicant-e2e", "python", str(web / "e2e" / "live_gate.py")]
    t0 = time.monotonic()
    with r.log.open("w") as f:
        mk = subprocess.run([*gate_py, "make"], cwd=ctx.p.root, env=ctx.env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=1500)
        f.write(mk.stdout)
    last = (mk.stdout.strip().splitlines() or ["{}"])[-1]
    try:
        ids = json.loads(last)
    except json.JSONDecodeError:
        ids = {}
    if mk.returncode != 0 or "gate_id" not in ids:
        r.note = "live_gate.py make failed"
        r.seconds = time.monotonic() - t0
        return [_status(r, mk.returncode or 1)]
    pw_out = ctx.out / "playwright"
    pw_out.mkdir(exist_ok=True)
    cmd = [*engine.cli("run"), "--rm", "--network", "host", "-v", f"{web}:/app", "-v", f"{pw_out}:/out", "-w", "/app",
           "-e", f"JOB_ID={ids['job_id']}", "-e", f"GATE_ID={ids['gate_id']}",
           "-e", "PLAYWRIGHT_JUNIT_OUTPUT_NAME=/out/junit.xml", PLAYWRIGHT_IMAGE,
           "npx", "playwright", "test", "--reporter=list,junit", *PLAYWRIGHT_SPECS]
    with r.log.open("a") as f:
        rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=1800).returncode
        fin = subprocess.run([*gate_py, "finish", ids["job_id"]], cwd=ctx.p.root, env=ctx.env, stdout=f,
                             stderr=subprocess.STDOUT, timeout=1200)
    r.seconds = time.monotonic() - t0
    if (pw_out / "junit.xml").is_file():
        shutil.copy(pw_out / "junit.xml", ctx.out / "junit-playwright.xml")
        r.passed, r.failed, r.skipped = _junit_counts(pw_out / "junit.xml")
    if fin.returncode != 0:
        r.note = f"live_gate.py finish failed ({ids['job_id']})"
        r.failed += 1
    return [_status(r, rc)]


def gateway_mode(p: Paths) -> str:
    """REPLICANT_GATEWAY_MODE as written in dev/compose/.env (what gateway-shim runs with); script when unset."""
    if p.env_file.is_file():
        for line in p.env_file.read_text().splitlines():
            if line.startswith("REPLICANT_GATEWAY_MODE="):
                return line.split("=", 1)[1].strip() or "script"
    return "script"


def set_gateway_mode(p: Paths, mode: str, log: Callable[[str], None]) -> bool:
    """Set REPLICANT_GATEWAY_MODE and recreate only gateway-shim, then wait until it is healthy again."""
    from replicant_cli import secrets
    secrets.set_env(p, {"REPLICANT_GATEWAY_MODE": mode})
    subprocess.run(compose.compose_cmd(p, "v0") + ["up", "-d", "--no-deps", "--force-recreate", "gateway-shim"],
                   capture_output=True, text=True, timeout=300)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        c = compose.containers().get("gateway-shim")
        if c and c.status == "running" and getattr(c, "health", "healthy") in ("healthy", "", None):
            log(f"gateway-shim is up in `{mode}` mode")
            return True
        time.sleep(3)
    log(f"WARNING: gateway-shim not healthy after switching to `{mode}`")
    return False


def e2e(p: Paths, *, only: list[str], fail_fast: bool, junit: str | None, log: Callable[[str], None]) -> int:
    t0 = time.monotonic()
    cs = compose.containers()
    need = ("control-plane-api", "gitea", "replicant-worker", "runner-local", "control-plane-web", "slack-mock")
    down = [n for n in need if not (cs.get(n) and cs[n].status == "running")]
    if down:
        log(f"the stack is not up ({', '.join(down)} not running): replicant dev up && replicant dev seed")
        return 1
    suites = [Suite(n, _run_driver, cwd=p.root / "tests" / "e2e") for n in E2E_DRIVERS]
    suites.append(Suite("playwright", _run_playwright))
    suites = _selected(suites, only)
    out = out_dir(p)
    # The E2Es use the scripted gateway (deterministic, no model cost): switch for the run, restore afterwards.
    mode = gateway_mode(p)
    if mode != "script":
        log(f"gateway mode is {mode!r}: switching to `script` for the E2Es (restored afterwards)")
        set_gateway_mode(p, "script", log)
    try:
        log(f"running {len(suites)} E2E suite(s) one at a time (shared stack); logs in {out}")
        results = run_suites(Ctx(p, out, dict(os.environ), log=log), suites, 1, fail_fast)
    finally:
        if mode != "script":
            set_gateway_mode(p, mode, log)
    print_table(results, time.monotonic() - t0, log)
    copy_junit(out, junit, log)
    return 1 if any(r.status in ("FAIL", "NOT RUN") for r in results) else 0


def m3(p: Paths, *, build: bool, log: Callable[[str], None]) -> int:
    cmd = [str(p.root / "tests" / "run_m3_pass.sh"), *([] if build else ["--no-build"])]
    log(f"running {' '.join(cmd[1:]) or '(build)'}: down -v, up, seed, E2E-01, SEC-EGRESS, chaos (15-20 min)")
    t0 = time.monotonic()
    rc = subprocess.run(cmd, cwd=p.root).returncode
    log(f"m3 {'PASS' if rc == 0 else 'FAIL'} in {time.monotonic() - t0:.0f}s")
    return rc
