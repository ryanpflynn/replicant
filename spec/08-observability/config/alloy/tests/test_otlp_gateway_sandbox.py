#!/usr/bin/env python3
"""Functional test for otlp-gateway-sandbox.alloy (WS-08), including the OTLP log fallback (R2-039).

    uv run python test_otlp_gateway_sandbox.py --alloy /path/to/alloy

Substitutions: otelcol.processor.k8sattributes is replaced by a transform that sets the attributes the processor
would derive from the pod (the test machine has no Kubernetes API); TLS removed; exporters replaced with
otelcol.exporter.debug and loki.echo. Everything else runs unmodified. Asserts: sandbox-claimed identity is
replaced, prompt content removed, secrets redacted, unrelated CLI metrics dropped, and fallback log lines reach Loki
format with the closed label set, pod-derived identity, principal_id and the T-1 trace ID.
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
import urllib.request
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "otlp-gateway-sandbox.alloy"
JOB, RUN = "job_01m5p7x7r0bt69j0h0fbxepw90", "run_gj3h3ep3phjfs9e23v768zrca5"

FAKE_K8S = '''otelcol.processor.transform "k8sfake" {
  error_mode = "ignore"
  trace_statements {
    context    = "resource"
    statements = [%(s)s]
  }
  metric_statements {
    context    = "resource"
    statements = [%(s)s]
  }
  log_statements {
    context    = "resource"
    statements = [%(s)s]
  }
  output {
    traces  = [otelcol.processor.filter.only_sandboxes.input]
    metrics = [otelcol.processor.filter.only_sandboxes.input]
    logs    = [otelcol.processor.filter.only_sandboxes.input]
  }
}
'''
SETS = ", ".join(f'`set(attributes["{k}"], "{v}")`' for k, v in {
    "k8s.namespace.name": "sbx-payments", "k8s.pod.name": "ar-gj3h3ep3phjfs9e23v768zrca5-x7k2p",
    "replicant.tenant_id": "payments", "replicant.job_id": JOB, "replicant.run_id": RUN, "replicant.role": "implementer",
    "replicant.release_bundle_id": "rb_ds5p3wmcj4dag2e9a8ncp4py6k", "replicant.workflow_type": "bugfix",
    "replicant.requester_id": "usr_01knk8dfr0fvjc7v0b9vhvkzbp", "replicant.managed_by": "agentrun-controller"}.items())


def attrs(d: dict) -> list:
    return [{"key": k, "value": {"stringValue": v}} for k, v in d.items()]


def build(td: Path) -> Path:
    s = CONFIG.read_text()
    a, b = s.index('otelcol.processor.k8sattributes "sandbox" {'), s.index("// 3. Drop anything")
    s = s[:a] + FAKE_K8S % {"s": SETS} + s[b:]
    s = s.replace("[otelcol.processor.k8sattributes.sandbox.input]", "[otelcol.processor.transform.k8sfake.input]")
    s = s.replace('"0.0.0.0:4318"', '"127.0.0.1:15318"')
    s = re.sub(r"\n    // Server certificate.*?\n    tls \{\n.*?\n    \}\n", "\n", s, flags=re.S)
    s = s.replace("[otelcol.exporter.otlp.core.input]", "[otelcol.exporter.debug.d.input]")
    s = s.replace("forward_to         = [loki.write.fallback.receiver]", "forward_to         = [loki.echo.e.receiver]")
    s = s[: s.index('loki.write "fallback"')]
    s += 'otelcol.exporter.debug "d" { verbosity = "detailed" }\nloki.echo "e" {}\n'
    s = s.replace("/etc/replicant/redaction/gitleaks-replicant.toml",
                  str(Path(__file__).resolve().parents[3] / "redaction" / "gitleaks-replicant.toml"))
    out = td / "sg.alloy"
    out.write_text(s)
    return out


def post(path: str, body: dict) -> None:
    req = urllib.request.Request(f"http://127.0.0.1:15318{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alloy", default=os.environ.get("ALLOY_BIN", "alloy"))
    args = ap.parse_args()
    env = {**os.environ, "REPLICANT_ENV": "prod", "CORE_OTLP_ENDPOINT": "x:1", "OTLP_TLS_DIR": "/nonexistent",
           "SANDBOX_OTLP_TLS_DIR": "/nonexistent", "LOKI_PUSH_URL": "http://l"}
    spans = {"resourceSpans": [{"resource": {"attributes": attrs({"replicant.job_id": "job_FORGED", "service.name": "x"})},
        "scopeSpans": [{"scope": {"name": "replicant.agent"}, "spans": [{"traceId": "fc0575170c8597ce9805552723c08d6e",
            "spanId": "a3c1e5f7092b4d6e", "name": "tool Bash", "kind": 1, "startTimeUnixNano": "1", "endTimeUnixNano": "2",
            "attributes": attrs({"replicant.tenant_id": "evil", "prompt": "secret prompt",
                                 "langfuse.observation.output": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"})}]}]}]}
    metrics = {"resourceMetrics": [{"resource": {"attributes": attrs({"service.name": "claude-code"})}, "scopeMetrics": [
        {"scope": {"name": "com.anthropic.claude_code"}, "metrics": [
            {"name": "claude_code.lines_of_code.count", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
             "dataPoints": [{"asInt": "5", "startTimeUnixNano": "1", "timeUnixNano": "2", "attributes": attrs({"type": "added"})}]}},
            {"name": "claude_code.cost.usage", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
             "dataPoints": [{"asDouble": 0.5, "startTimeUnixNano": "1", "timeUnixNano": "2",
                             "attributes": attrs({"model": "m", "session.id": "s", "tool_name": "Bash"})}]}}]}]}]}
    line = json.dumps({"level": "info", "stream": "transcript", "event": "tool_result", "run_id": RUN, "msg": "ok"})
    logs = {"resourceLogs": [{"resource": {"attributes": attrs({"replicant.job_id": "job_FORGED"})}, "scopeLogs": [
        {"scope": {"name": "replicant.uploader"}, "logRecords": [{"timeUnixNano": "1792822345000000000",
            "body": {"stringValue": line}, "attributes": attrs({"replicant.container": "uploader", "sbx_job_id": "job_FORGED"})}]}]}]}
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        cfg = build(td)
        log = td / "out.log"
        with log.open("w") as fh:
            p = subprocess.Popen([args.alloy, "run", str(cfg), f"--storage.path={td}/data",
                                  "--server.http.listen-addr=127.0.0.1:12394", "--stability.level=experimental"],
                                 stdout=fh, stderr=subprocess.STDOUT, env=env)
            time.sleep(5)
            post("/v1/traces", spans)
            post("/v1/metrics", metrics)
            post("/v1/logs", logs)
            time.sleep(6)
            p.terminate()
            p.wait(timeout=10)
        out = log.read_text()
    entries = [json.loads(l) for l in out.splitlines() if l.startswith("{") and "received log entry" in l]
    lab = dict(re.findall(r'(\w+)="([^"]*)"', entries[0]["labels"])) if entries else {}
    meta = json.loads(entries[0].get("structured_metadata") or "{}") if entries else {}
    checks = [
        ("no config/OTTL errors", not [l for l in out.splitlines() if '"level":"error"' in l
                                        and "otelcol.exporter.otlp.core" not in l]),
        ("forged job replaced on spans", JOB in out and "job_FORGED" not in out),
        ("prompt attribute removed", "secret prompt" not in out),
        ("bearer redacted", "[REDACTED:http-bearer-token]" in out),
        ("unrelated CLI metric dropped", "lines_of_code" not in out),
        ("cost metric kept without session.id/tool_name", "claude_code.cost.usage" in out and "session.id" not in out
         and "tool_name" not in out),
        ("fallback log reached Loki stage", len(entries) == 1),
        ("fallback labels closed set", set(lab) - {"__tenant_id__"} == {"env", "cluster", "namespace", "service", "tenant",
                                                                         "workflow_type", "role", "level", "source"}),
        ("fallback tenant routing", lab.get("__tenant_id__") == "sandbox" and lab.get("tenant") == "payments"),
        ("fallback transcript from uploader", lab.get("source") == "transcript"),
        ("fallback line restored", entries and json.loads(entries[0]["entry"]).get("event") == "tool_result"),
        ("fallback identity pod-derived", meta.get("job_id") == JOB and meta.get("trace_id") == "fc0575170c8597ce9805552723c08d6e"),
        ("fallback principal_id", meta.get("principal_id") == "usr_01knk8dfr0fvjc7v0b9vhvkzbp"),
    ]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    if not all(ok for _, ok in checks):
        print(out[-5000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
