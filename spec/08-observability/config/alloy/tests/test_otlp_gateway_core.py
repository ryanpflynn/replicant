#!/usr/bin/env python3
"""Functional test for otlp-gateway-core.alloy (WS-08). Compiles every OTTL statement by running Alloy, posts
OTLP/HTTP JSON spans and metrics, and asserts on what reaches the Tempo, Langfuse and Mimir branches.

    uv run python test_otlp_gateway_core.py --alloy /path/to/alloy

Substitutions: gRPC TLS removed, ports moved to 127.0.0.1:1431x, Tempo/Langfuse exporters replaced with
otelcol.exporter.debug (detailed), Mimir remote write replaced with prometheus.echo. Processors run unmodified.
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

CONFIG = Path(__file__).resolve().parent.parent / "otlp-gateway-core.alloy"
JOB = "job_01m5p7x7r0bt69j0h0fbxepw90"
TRACE = "fc0575170c8597ce9805552723c08d6e"


def attrs(d: dict) -> list:
    return [{"key": k, "value": {"stringValue": v} if isinstance(v, str) else {"intValue": str(v)}}
            for k, v in d.items()]


COMMON = {"replicant.job_id": JOB, "replicant.tenant_id": "payments", "replicant.run_id": "run_gj3h3ep3phjfs9e23v768zrca5",
          "replicant.release_bundle_id": "rb_ds5p3wmcj4dag2e9a8ncp4py6k", "replicant.workflow_type": "bugfix",
          "replicant.node_key": "implementer", "replicant.cycle": "0", "replicant.role": "implementer"}


def span(sid: str, name: str, a: dict) -> dict:
    return {"traceId": TRACE, "spanId": sid, "name": name, "kind": 1, "startTimeUnixNano": "1792822345000000000",
            "endTimeUnixNano": "1792822346000000000", "attributes": attrs(a)}


SPANS = {"resourceSpans": [
    {"resource": {"attributes": attrs({"service.name": "replicant-sandbox", "replicant.generation_source": "cli"})},
     "scopeSpans": [
         {"scope": {"name": "replicant.agent"}, "spans": [span("a3c1e5f7092b4d6e", "tool Bash", {
             **COMMON, "langfuse.observation.type": "tool", "langfuse.observation.input": "go test ./...",
             "langfuse.observation.output": "ok"})]},
         {"scope": {"name": "com.anthropic.claude_code"}, "spans": [
             span("b3c1e5f7092b4d6e", "claude_code.llm_request", {**COMMON, "model": "claude-sonnet-4-5",
                  "input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 1000,
                  "cache_creation_tokens": 0}),
             span("c3c1e5f7092b4d6e", "claude_code.tool", COMMON)]}]},
    {"resource": {"attributes": attrs({"service.name": "replicant-gateway-shim"})},
     "scopeSpans": [{"scope": {"name": "replicant.gateway"}, "spans": [
         span("e3c1e5f7092b4d6e", "gateway.request", {**COMMON, "gen_ai.request.model": "anthropic/claude-sonnet-4-5-20250929",
              "gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 300, "replicant.cost_micros": 8100})]}]},
    {"resource": {"attributes": attrs({"service.name": "replicant-sandbox"})},
     "scopeSpans": [{"scope": {"name": "replicant.agent"}, "spans": [span("f3c1e5f7092b4d6e", "tool Restricted", {
         **COMMON, "replicant.tenant_id": "hr-systems", "langfuse.observation.type": "tool"})]}]},
    {"resource": {"attributes": attrs({"service.name": "replicant-worker"})},
     "scopeSpans": [{"scope": {"name": "opentelemetry.instrumentation.httpx"}, "spans": [
         span("d3c1e5f7092b4d6e", "GET /v1/runs/{run_id}", {"replicant.job_id": JOB,
              "url.full": "https://runner.prod.replicant.internal/v1/runs/x?X-Amz-Signature=abc"})]}]}]}

METRICS = {"resourceMetrics": [{"resource": {"attributes": attrs({"service.name": "control-plane-api"})},
    "scopeMetrics": [{"scope": {"name": "x"}, "metrics": [{"name": "http.server.request.duration", "unit": "s",
        "histogram": {"aggregationTemporality": 2, "dataPoints": [{"attributes": attrs({
            "http.route": "/v1/jobs", "http.request.method": "POST", "http.response.status_code": 201,
            "replicant.job_id": JOB, "server.address": "x"}), "startTimeUnixNano": "1792822345000000000",
            "timeUnixNano": "1792822346000000000", "count": "1", "sum": 0.2, "bucketCounts": ["0", "1"],
            "explicitBounds": [0.1]}]}}]}]}]}


def build(td: Path) -> Path:
    s = CONFIG.read_text()
    s = re.sub(r"\n    tls \{\n      ca_file\s+= .*?\n    \}\n", "\n", s, flags=re.S)
    s = s.replace('"0.0.0.0:4317"', '"127.0.0.1:14317"').replace('"0.0.0.0:4318"', '"127.0.0.1:14318"')
    s = s.replace("traces = [otelcol.exporter.otlp.tempo.input]", "traces = [otelcol.exporter.debug.tempo.input]")
    s = s.replace("traces = [otelcol.exporter.otlphttp.langfuse.input]",
                  "traces = [otelcol.exporter.debug.langfuse.input]")
    s = re.sub(r"forward_to\s+= \[prometheus\.remote_write\.mimir\.receiver\]",
               "forward_to = [prometheus.echo.m.receiver]", s)
    s += ('\notelcol.exporter.debug "tempo" { verbosity = "detailed" }\n'
          'otelcol.exporter.debug "langfuse" { verbosity = "detailed" }\nprometheus.echo "m" {}\n')
    out = td / "gw.alloy"
    out.write_text(s)
    return out


def post(path: str, body: dict) -> None:
    req = urllib.request.Request(f"http://127.0.0.1:14318{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alloy", default=os.environ.get("ALLOY_BIN", "alloy"))
    args = ap.parse_args()
    env = {**os.environ, "REPLICANT_ENV": "prod", "OTLP_TLS_DIR": "/nonexistent", "TEMPO_OTLP_ENDPOINT": "t:4317",
           "LANGFUSE_OTLP_TRACES_URL": "http://l/x", "LANGFUSE_BASIC_AUTH": "abc",
           "MIMIR_PUSH_URL": "http://127.0.0.1:1/api/v1/push",
           "LANGFUSE_EXCLUDED_TENANTS_REGEX": "^(hr-systems|legal)$"}
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        cfg = build(td)
        log = td / "out.log"
        with log.open("w") as fh:
            p = subprocess.Popen([args.alloy, "run", str(cfg), f"--storage.path={td}/data",
                                  "--server.http.listen-addr=127.0.0.1:12395", "--stability.level=experimental"],
                                 stdout=fh, stderr=subprocess.STDOUT, env=env)
            time.sleep(5)
            post("/v1/traces", SPANS)
            post("/v1/metrics", METRICS)
            time.sleep(8)
            p.terminate()
            p.wait(timeout=10)
        out = log.read_text()

    def section(component: str) -> str:
        return "\n".join(l for l in out.splitlines() if f'"{component}"' in l or component in l)

    lf, tempo = section("otelcol.exporter.debug.langfuse"), section("otelcol.exporter.debug.tempo")
    echo = section("prometheus.echo")
    checks = [
        ("no OTTL/config errors", '"level":"error"' not in out),
        ("langfuse gets tool observation", "tool Bash" in lf),
        ("gateway.request becomes generation", "gateway.request" in lf and "Str(generation)" in lf),
        ("generation cost_details", "0.008100" in lf),
        ("llm_request stays span", "claude_code.llm_request" in lf),
        ("langfuse session id = job_id", "langfuse.session.id" in lf and JOB in lf),
        ("langfuse tags", "tenant:payments" in lf and "bundle:rb_ds5p3wmcj" in lf),
        ("langfuse drops claude_code.tool", r"claude_code.tool\n" not in lf),
        ("tempo keeps claude_code.tool", r"claude_code.tool\n" in tempo),
        ("langfuse drops http spans", "GET /v1/runs" not in lf),
        ("tempo gets http span", "GET /v1/runs" in tempo),
        ("tempo query string removed", "X-Amz-Signature" not in tempo),
        ("tempo content stripped", "langfuse.observation.input" not in tempo),
        ("metric renamed", "replicant_http_server_request_duration" in out),
        ("job_id not a metric label", 'job_id="' not in echo),
        ("forbidden label marked", 'forbidden_label' in echo and 'job_id' in echo),
        ("restricted tenant excluded from Langfuse", "tool Restricted" not in lf),
        ("restricted tenant still in Tempo", "tool Restricted" in tempo),
    ]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    if not all(ok for _, ok in checks):
        print(out[-6000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
