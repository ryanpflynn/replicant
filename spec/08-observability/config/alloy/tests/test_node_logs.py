#!/usr/bin/env python3
"""Functional test for node-logs.alloy (WS-08). Run in CI with the pinned Alloy binary:

    uv run python test_node_logs.py --alloy /path/to/alloy

The Kubernetes discovery section is replaced by static local.file_match targets that carry the same temporary
labels discovery.relabel produces; loki.write is replaced by loki.echo. Every other stage runs unmodified.
Asserts: closed label set, Loki tenant routing (sandbox/platform/audit), identity from pod labels (a forged job_id
in a sandbox line is ignored), C-IDS regex validation of metadata, and trace_id = Rule T-1 test vector.
Verified passing with Alloy v1.21.2 on 2026-10-24.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE.parent / "node-logs.alloy"
RULES = HERE.parents[2] / "redaction" / "gitleaks-replicant.toml"
LABELS = {"env", "cluster", "namespace", "service", "tenant", "workflow_type", "role", "level", "source"}
JOB = "job_01m5p7x7r0bt69j0h0fbxepw90"
TRACE = "fc0575170c8597ce9805552723c08d6e"  # C-IDS section 1 test vector

STATIC = """
local.file_match "sandbox" {{
  path_targets = [{{"__path__" = "{sb}", "namespace" = "sbx-payments", "pod" = "ar-gj3h3ep3phjfs9e23v768zrca5-x7k2p",
    "container" = "uploader", "sbx_tenant" = "payments", "sbx_job_id" = "{job}",
    "sbx_run_id" = "run_gj3h3ep3phjfs9e23v768zrca5", "sbx_role" = "implementer",
    "sbx_bundle" = "rb_ds5p3wmcj4dag2e9a8ncp4py6k", "sbx_acting_for" = "usr_01knk8dfr0fvjc7v0b9vhvkzbp"}},
    {{"__path__" = "{sb2}", "namespace" = "sbx-acme-hr", "pod" = "ar-hj3h3ep3phjfs9e23v768zrca5-x7k2p",
    "container" = "agent", "sbx_tenant" = "acme-hr", "sbx_job_id" = "{job}",
    "sbx_run_id" = "run_hj3h3ep3phjfs9e23v768zrca5", "sbx_role" = "implementer",
    "sbx_bundle" = "rb_ds5p3wmcj4dag2e9a8ncp4py6k"}}]
}}
local.file_match "platform" {{
  path_targets = [{{"__path__" = "{pl}", "namespace" = "replicant-workers", "pod" = "replicant-worker-7d9f-abcde",
    "container" = "worker", "k8s_app" = "replicant-worker"}}]
}}
"""


def cri(i: int, obj: dict) -> str:
    return f"2026-10-24T08:00:{i:02d}.000000000Z stdout F {json.dumps(obj)}\n"


def build(td: Path) -> Path:
    src = CONFIG.read_text()
    start, end = src.index('discovery.kubernetes "pods"'), src.index('loki.source.file "sandbox"')
    sb, pl, sb2 = td / "sb.log", td / "pl.log", td / "sb2.log"
    sb2.write_text(cri(5, {"level": "info", "msg": "restricted"}))
    sb.write_text(cri(1, {"level": "info", "stream": "transcript", "event": "tool_result",
                          "span_id": "a3c1e5f7092b4d6e", "node_key": "implementer", "cycle": "0",
                          "phase": "implementing", "job_id": "job_aaaaaaaaaaaaaaaaaaaaaaaaaa", "msg": "ok"}))
    pl.write_text(cri(2, {"level": "INFO", "tenant_id": "payments", "workflow_type": "bugfix", "job_id": JOB,
                          "activity_id": "implementer.c0.n1.run", "gate_id": "bogus", "msg": "activity started"})
                  + cri(3, {"level": "info", "log_type": "audit", "tenant_id": "payments",
                            "principal_id": "usr_01knk8dfr0fvjc7v0b9vhvkzbp", "msg": "gate.decided"})
                  + cri(4, {"level": "error", "tenant_id": "NotATenant!!", "msg": "boom"}))
    cfg = src[:start] + STATIC.format(sb=sb, sb2=sb2, pl=pl, job=JOB) + src[end:]
    cfg = cfg.replace("loki.write.default.receiver", "loki.echo.e.receiver")
    cfg = cfg[:cfg.index('loki.write "default"')] + 'loki.echo "e" {}\n'
    cfg = cfg.replace("/etc/replicant/redaction/gitleaks-replicant.toml", str(RULES))
    for name in ("sandbox", "platform"):
        cfg = cfg.replace(f'loki.source.file "{name}" {{', f'loki.source.file "{name}" {{\n  tail_from_end = false')
    out = td / "t.alloy"
    out.write_text(cfg)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alloy", default=os.environ.get("ALLOY_BIN", "alloy"))
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        cfg = build(td)
        env = {**os.environ, "REPLICANT_ENV": "prod", "REPLICANT_CLUSTER": "core",
               "LOKI_RESTRICTED_TENANTS_REGEX": "acme-hr|acme-health"}
        log = td / "out.log"
        with log.open("w") as fh:
            p = subprocess.Popen([args.alloy, "run", str(cfg), f"--storage.path={td}/data",
                                  "--server.http.listen-addr=127.0.0.1:12396"], stdout=fh, stderr=subprocess.STDOUT,
                                 env=env)
            deadline = time.time() + 30
            while time.time() < deadline and log.read_text().count("received log entry") < 5:
                time.sleep(0.5)
            p.terminate()
            p.wait(timeout=10)
        entries = [json.loads(l) for l in log.read_text().splitlines()
                   if l.startswith("{") and "received log entry" in l]

    def labels(e: dict) -> dict:
        return dict(re.findall(r'(\w+)="([^"]*)"', e["labels"]))

    def meta(e: dict) -> dict:
        return json.loads(e.get("structured_metadata") or "{}")

    by_msg = {json.loads(e["entry"])["msg"]: e for e in entries}
    checks = []
    sb = by_msg["ok"]
    checks.append(("sandbox tenant routing", labels(sb)["__tenant_id__"] == "sandbox"))
    checks.append(("sandbox job from pod label", meta(sb)["job_id"] == JOB))
    checks.append(("sandbox trace id T-1", meta(sb)["trace_id"] == TRACE))
    checks.append(("sandbox source", labels(sb)["source"] == "transcript"))
    checks.append(("uploader service name", labels(sb)["service"] == "replicant-sandbox-uploader"))
    checks.append(("principal_id from acting-for label", meta(sb).get("principal_id") == "usr_01knk8dfr0fvjc7v0b9vhvkzbp"))
    act = by_msg["activity started"]
    checks.append(("platform routing", labels(act)["__tenant_id__"] == "platform"))
    checks.append(("invalid gate_id dropped", "gate_id" not in meta(act)))
    checks.append(("platform trace derived", meta(act)["trace_id"] == TRACE))
    checks.append(("level normalized", labels(act)["level"] == "info"))
    aud = by_msg["gate.decided"]
    checks.append(("audit routing", labels(aud)["__tenant_id__"] == "audit"))
    rst = by_msg["restricted"]
    checks.append(("restricted tenant -> own Loki tenant (R1V-052)", labels(rst)["__tenant_id__"] == "sandbox-acme-hr"))
    bad = by_msg["boom"]
    checks.append(("invalid tenant -> replicant-system", labels(bad)["tenant"] == "replicant-system"))
    for e in entries:
        checks.append(("closed label set", set(labels(e)) - {"__tenant_id__"} == LABELS))
    failed = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(("PASS " if ok else "FAIL ") + n)
    return 1 if failed or len(entries) != 5 else 0


if __name__ == "__main__":
    sys.exit(main())
