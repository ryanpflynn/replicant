import subprocess
import sys
from pathlib import Path

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import replicant_telemetry as ft
from replicant_telemetry import core, runtime

JOB = "job_01m5p7x7r0bt69j0h0fbxepw90"


def test_spec_self_test_runs():
    r = subprocess.run([sys.executable, str(Path(core.__file__))], capture_output=True, text=True)
    assert r.returncode == 0 and "self-test passed" in r.stdout, r.stderr[-800:]


def test_vectors():
    assert ft.job_trace_id(JOB) == "fc0575170c8597ce9805552723c08d6e"
    assert ft.job_root_span_id(JOB) == "2f9b8c2c87b35ba4"


def test_job_span_in_job_trace(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    runtime.shutdown()
    exp = InMemorySpanExporter()
    main, root = runtime._providers("test")  # noqa: SLF001
    main.add_span_processor(SimpleSpanProcessor(exp))
    root.add_span_processor(SimpleSpanProcessor(exp))
    assert runtime.emit_job_root(JOB, "payments") == "fc0575170c8597ce9805552723c08d6e"
    with runtime.job_span("activity x", JOB, tenant_id="payments"):
        with runtime.job_span("inner", JOB):
            pass
    spans = {s.name: s for s in exp.get_finished_spans()}
    assert spans["job"].context.span_id == int("2f9b8c2c87b35ba4", 16)
    assert spans["activity x"].parent.span_id == int("2f9b8c2c87b35ba4", 16)
    assert spans["inner"].parent.span_id == spans["activity x"].context.span_id
    assert {s.context.trace_id for s in spans.values()} == {int("fc0575170c8597ce9805552723c08d6e", 16)}
    assert spans["activity x"].attributes["replicant.tenant_id"] == "payments"
    runtime.shutdown()


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    runtime.shutdown()
    assert runtime.emit_job_root(JOB, "payments") is None
    with runtime.job_span("x", JOB) as s:
        assert s is None
