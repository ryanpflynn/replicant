#!/usr/bin/env python3
"""Builds the provisioned Grafana dashboard JSON files (WS-08). Run: python3 build_dashboards.py
Output files are committed; CI regenerates them and fails on diff. Panel queries are documented in
../dashboards.md; every query here is copied from there.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIMIR = {"type": "prometheus", "uid": "mimir"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}
PG = {"type": "grafana-postgresql-datasource", "uid": "replicant-pg"}


def panel(pid, title, kind, ds, targets, x, y, w, h, desc, unit=None, extra=None):
    p = {"id": pid, "title": title, "type": kind, "datasource": ds, "description": desc,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
         "fieldConfig": {"defaults": {}, "overrides": []}, "options": {}}
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if extra:
        p.update(extra)
    return p


def prom(expr, legend="", ref="A", exemplar=False):
    return {"refId": ref, "datasource": MIMIR, "expr": expr, "legendFormat": legend, "exemplar": exemplar}


def loki(expr, ref="A"):
    return {"refId": ref, "datasource": LOKI, "expr": expr, "queryType": "range"}


def sql(q, ref="A", fmt="table"):
    return {"refId": ref, "datasource": PG, "rawSql": q, "format": fmt, "rawQuery": True, "editorMode": "code"}


def dashboard(uid, title, panels, templating, tags, links=None):
    return {"uid": uid, "title": title, "tags": tags, "timezone": "utc", "schemaVersion": 41, "version": 1,
            "editable": False, "graphTooltip": 1, "time": {"from": "now-24h", "to": "now"},
            "refresh": "1m", "templating": {"list": templating}, "panels": panels,
            "links": links or [], "annotations": {"list": [{
                "name": "Job terminal (failed)", "datasource": PG, "enable": False, "iconColor": "#c4162a",
                "rawQuery": "SELECT terminal_at AS time, job_id AS text FROM jobs_read.jobs "
                            "WHERE terminal_outcome = 'failed' AND $__timeFilter(terminal_at)"}]}}


def var_query(name, label, query, ds=MIMIR, multi=True, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": ds,
            "query": {"query": query, "refId": name}, "refresh": 2, "multi": multi,
            "includeAll": include_all, "current": {}}


ENV_TENANT = [
    var_query("tenant", "Tenant", 'label_values(replicant_jobs_terminal_total, tenant)'),
    var_query("workflow_type", "Workflow type", 'label_values(replicant_jobs_terminal_total, workflow_type)'),
]

overview = dashboard("replicant-overview", "Replicant overview", [
    panel(1, "First progress within 60 s (SLO-1, target 99%)", "stat", MIMIR,
          [prom("1 - replicant:first_progress_bad:ratio_rate1h")], 0, 0, 6, 4,
          "Share of jobs whose first non-intake phase was visible within 60 s of the trigger, last hour.",
          "percentunit"),
    panel(2, "Trigger to first sandbox ready p95 (SLO-2, target < 90 s)", "stat", MIMIR,
          [prom("replicant:sandbox_ready_seconds:p95_1h")], 6, 0, 6, 4,
          "p95 of trigger -> first AgentRun AgentRunning, last hour.", "s"),
    panel(3, "Control-plane error budget remaining (SLO-3, 99.9% / 30 d)", "stat", MIMIR,
          [prom("1 - (sum(increase(replicant_http_server_request_duration_seconds_count{service=\"control-plane-api\",status_code=~\"5..\"}[30d])) / sum(increase(replicant_http_server_request_duration_seconds_count{service=\"control-plane-api\"}[30d]))) / 0.001")],
          12, 0, 6, 4, "1 = untouched budget, 0 = exhausted.", "percentunit"),
    panel(4, "Jobs waiting on humans / stalled", "stat", MIMIR,
          [prom("sum(replicant_jobs_in_status{status=\"waiting_human\"})", "waiting_human", "A"),
           prom("sum(replicant_jobs_stalled)", "stalled", "B")], 18, 0, 6, 4,
          "Gauges from sys:projection-reconciler (every 2 min). Stalled = running, no progress 30 min."),
    panel(5, "Jobs finished per hour by outcome", "timeseries", MIMIR,
          [prom("sum by (outcome) (increase(replicant_jobs_terminal_total{tenant=~\"$tenant\",workflow_type=~\"$workflow_type\"}[1h]))", "{{outcome}}")],
          0, 4, 12, 8, "Terminal statuses per hour (C-IDS section 6).", "short"),
    panel(6, "Job failure ratio by workflow type (1 h)", "timeseries", MIMIR,
          [prom("sum by (workflow_type) (increase(replicant_jobs_terminal_total{outcome=\"failed\",tenant=~\"$tenant\"}[1h])) / clamp_min(sum by (workflow_type) (increase(replicant_jobs_terminal_total{tenant=~\"$tenant\"}[1h])), 1)", "{{workflow_type}}")],
          12, 4, 12, 8, "Alert ReplicantJobFailureRateHigh at 25% with >= 8 jobs.", "percentunit"),
    panel(7, "Run failures by reason (6 h)", "barchart", MIMIR,
          [prom("sort_desc(sum by (reason) (increase(replicant_controller_phase_transitions_total{phase=\"Failed\"}[6h])))", "{{reason}}")],
          0, 12, 12, 8, "C-CRD section 4 reasons. Infrastructure reasons page WS-02; profile reasons ticket WS-03.", "short",
          {"options": {"orientation": "horizontal"}}),
    panel(8, "Sandbox startup p95 by segment and isolation", "timeseries", MIMIR,
          [prom("histogram_quantile(0.95, sum by (le, phase, isolation) (rate(replicant_controller_run_startup_seconds_bucket[30m])))", "{{isolation}} {{phase}}")],
          12, 12, 12, 8, "Creation -> scheduled -> workspace_ready -> agent_running (WS-02 segments).", "s"),
    panel(9, "LLM spend per hour by tenant (USD)", "timeseries", MIMIR,
          [prom("topk(10, replicant:llm_cost_usd:increase1h)", "{{tenant}}")], 0, 20, 12, 8,
          "From replicant_gateway_cost_micros_total. The cost ledger (Postgres cost schema) is authoritative.", "currencyUSD"),
    panel(10, "Open gates by kind and oldest age", "table", MIMIR,
          [prom("sum by (tenant, gate_kind) (replicant_gates_open)", "", "A"),
           prom("max by (tenant, gate_kind) (replicant_gate_oldest_open_age_seconds)", "", "B")], 12, 20, 12, 8,
          "Approval backlog (D-19).", None, {"transformations": [{"id": "merge"}]}),
    panel(11, "Temporal task queue backlog age", "timeseries", MIMIR,
          [prom("max by (task_queue) (approximate_backlog_age_seconds{task_queue=~\"replicant-.+\"})", "{{task_queue}}")],
          0, 28, 12, 8, "Above 60 s pages (TemporalWorkflowTaskBacklog).", "s"),
    panel(12, "Error logs per service (5 m rate)", "timeseries", MIMIR,
          [prom("topk(10, replicant:log_errors:rate5m)", "{{service}}")], 12, 28, 12, 8,
          "Loki recording rule replicant:log_errors:rate5m; click through to Explore.", "short"),
    panel(13, "Recent failed jobs", "table", PG,
          [sql("SELECT j.job_id, j.tenant_id, j.workflow_type, j.status, j.updated_at, "
               "substr(encode(sha256(convert_to('replicant.job.trace.v1:' || j.job_id, 'UTF8')), 'hex'), 1, 32) AS trace_id "
               "FROM jobs_read.jobs j WHERE j.status = 'failed' AND $__timeFilter(j.updated_at) "
               "AND ('$tenant' = '$__all' OR j.tenant_id IN ($tenant)) ORDER BY j.updated_at DESC LIMIT 50")],
          0, 36, 24, 10, "Click job_id for the job drilldown.", None,
          {"fieldConfig": {"defaults": {}, "overrides": [{"matcher": {"id": "byName", "options": "job_id"},
            "properties": [{"id": "links", "value": [{"title": "Job drilldown",
              "url": "/d/replicant-job/job?var-job_id=${__value.raw}"}]}]}]}}),
], ENV_TENANT, ["replicant", "overview"])

job = dashboard("replicant-job", "Job drilldown", [
    panel(1, "Job", "table", PG,
          [sql("SELECT j.job_id, j.tenant_id, j.workflow_type, j.status, j.phase, j.release_bundle_id, "
               "j.created_at, j.updated_at FROM jobs_read.jobs j WHERE j.job_id = '$job_id'")],
          0, 0, 16, 4, "Read model row (C-STATE section 2).", None),
    panel(2, "Jump to", "text", None, [], 16, 0, 8, 4, "One click to every store holding this job.", None,
          {"options": {"mode": "markdown", "content":
            "[Control plane](https://app.prod.replicant.internal/jobs/$job_id) | "
            "[Temporal](https://temporal.prod.replicant.internal/namespaces/replicant-prod/workflows?query=ReplicantJobId%3D%22$job_id%22) | "
            "[Langfuse trace](https://langfuse.prod.replicant.internal/project/replicant-prod/traces/$trace_id) | "
            "[Langfuse session](https://langfuse.prod.replicant.internal/project/replicant-prod/sessions/$job_id) | "
            "[Tempo trace](/explore?left=%7B%22datasource%22:%22tempo%22,%22queries%22:%5B%7B%22refId%22:%22A%22,%22queryType%22:%22traceql%22,%22query%22:%22$trace_id%22%7D%5D%7D)"}}),
    panel(3, "Timeline", "table", PG,
          [sql("SELECT t.seq, t.at, t.type, t.summary, t.actor FROM jobs_read.job_timeline t "
               "WHERE t.job_id = '$job_id' ORDER BY t.seq")], 0, 4, 12, 12,
          "Workflow-generated timeline (never model text).", None),
    panel(4, "Runs", "table", PG,
          [sql("SELECT r.run_id, r.node_key, r.cycle, r.ordinal, r.role, r.phase, r.reason, r.isolation, "
               "r.started_at, r.finished_at FROM jobs_read.job_runs r WHERE r.job_id = '$job_id' ORDER BY r.started_at")],
          12, 4, 12, 12, "AgentRuns (Runner GET /v1/runs?job_id= is the live source).", None),
    panel(5, "Logs (platform and sandbox)", "logs", LOKI,
          [loki("{env=\"prod\"} | job_id=\"$job_id\" | level=~\"$level\"")], 0, 16, 24, 12,
          "Structured metadata job_id (C-IDS section 9). Derived fields link trace_id to Tempo and Langfuse.", None),
    panel(6, "Transcript events", "logs", LOKI,
          [loki("{source=\"transcript\"} | job_id=\"$job_id\"")], 0, 28, 24, 12,
          "Redacted, truncated transcript events shipped by the sandbox uploader. Full transcript: S3 transcripts bucket.",
          None),
    panel(7, "Trace", "traces", TEMPO,
          [{"refId": "A", "datasource": TEMPO, "queryType": "traceql", "query": "$trace_id"}], 0, 40, 24, 14,
          "One trace per job (C-IDS T-1).", None),
    panel(8, "Cost by run (ledger)", "table", PG,
          [sql("SELECT run_id, sum(cost_micros) / 1e6 AS usd, sum(input_tokens) AS input_tokens, "
               "sum(output_tokens) AS output_tokens FROM cost.usage_events WHERE job_id = '$job_id' "
               "GROUP BY run_id ORDER BY usd DESC")], 0, 54, 12, 8, "Postgres cost schema (WS-03).", None),
    panel(9, "Approvals", "table", PG,
          [sql("SELECT g.gate_id, g.kind, d.decision, d.principal_id, d.channel, d.decided_at, g.presented_digest "
               "FROM approvals.gates g LEFT JOIN approvals.gate_decisions d USING (gate_id) "
               "WHERE g.job_id = '$job_id' ORDER BY g.opened_at")], 12, 54, 12, 8,
          "Who decided, where, and the digest of what they saw (audit.md section 5).", None),
], [
    {"name": "job_id", "label": "job_id", "type": "textbox", "query": "", "current": {"value": ""}},
    {"name": "trace_id", "label": "trace_id", "type": "query", "datasource": PG, "hide": 2, "refresh": 1,
     "query": "SELECT substr(encode(sha256(convert_to('replicant.job.trace.v1:' || j, 'UTF8')), 'hex'), 1, 32) "
              "FROM (SELECT '$job_id' AS j) s WHERE j ~ '^job_[0-9a-hjkmnp-tv-z]{26}$'"},
    {"name": "level", "label": "Level", "type": "custom", "query": "debug,info,warn,error", "multi": True,
     "includeAll": True, "current": {"text": "All", "value": "$__all"}},
], ["replicant", "job"])

qa = dashboard("replicant-qa", "QA/QE", [
    panel(1, "QA runs by verdict", "timeseries", MIMIR,
          [prom("sum by (outcome) (increase(replicant_qa_runs_total{tenant=~\"$tenant\"}[1h]))", "{{outcome}}")],
          0, 0, 12, 8, "Which verdicts QA produces (WS-05 replicant_qa_runs_total).", "short"),
    panel(2, "QA first-pass rate by workflow type (1 d)", "timeseries", MIMIR,
          [prom("sum by (workflow_type) (increase(replicant_qa_first_pass_total{outcome=\"pass\",tenant=~\"$tenant\"}[1d])) / clamp_min(sum by (workflow_type) (increase(replicant_qa_first_pass_total{tenant=~\"$tenant\"}[1d])), 1)", "{{workflow_type}}")],
          12, 0, 12, 8, "Single emitter: replicant-projector (R1-201).", "percentunit"),
    panel(3, "Stage duration p95 by qa_stage", "timeseries", MIMIR,
          [prom("histogram_quantile(0.95, sum by (le, qa_stage) (rate(replicant_qa_stage_duration_seconds_bucket{tenant=~\"$tenant\"}[1h])))", "{{qa_stage}}")],
          0, 8, 12, 8, "Where QA time goes (C-IDS qa_stage, CCR-05-03).", "s"),
    panel(4, "Failing checks by stage and cause", "barchart", MIMIR,
          [prom("sort_desc(sum by (qa_stage, reason) (increase(replicant_qa_checks_total{tenant=~\"$tenant\"}[1d])))", "{{qa_stage}} {{reason}}")],
          12, 8, 12, 8, "Which checks fail and why.", "short", {"options": {"orientation": "horizontal"}}),
    panel(5, "Environment failures (operator-facing)", "timeseries", MIMIR,
          [prom("sum by (reason) (increase(replicant_qa_environment_failures_total{tenant=~\"$tenant\"}[1h]))", "{{reason}}")],
          0, 16, 12, 8, "Failures caused by the environment, not the change.", "short"),
    panel(6, "Flake reruns and fix iterations", "timeseries", MIMIR,
          [prom("sum by (outcome) (increase(replicant_qa_flake_reruns_total{tenant=~\"$tenant\"}[1h]))", "rerun {{outcome}}", "A"),
           prom("histogram_quantile(0.9, sum by (le) (rate(replicant_qa_fix_iterations_bucket{tenant=~\"$tenant\"}[1d])))", "fix iterations p90", "B")],
          12, 16, 12, 8, "Flakiness and QA-driven repair loops.", "short"),
    panel(7, "QA run wall time p95 by isolation", "timeseries", MIMIR,
          [prom("histogram_quantile(0.95, sum by (le, isolation) (rate(replicant_qa_run_duration_seconds_bucket{tenant=~\"$tenant\"}[1h])))", "{{isolation}}")],
          0, 24, 24, 8, "Runtime comparison for QA workloads (D-06).", "s"),
    panel(8, "Tests deleted or newly skipped", "timeseries", MIMIR,
          [prom("sum by (workflow_type) (increase(replicant_qa_tests_deleted_or_skipped_total{tenant=~\"$tenant\"}[1d]))", "{{workflow_type}}")],
          0, 32, 12, 8, "QA runs whose test inventory shows removed tests or new skips (R2-006, XD-W2-05-02).", "short"),
    panel(9, "Checkpoint stages reused on retry", "timeseries", MIMIR,
          [prom("sum by (qa_stage) (increase(replicant_qa_checkpoint_stages_reused_total{tenant=~\"$tenant\"}[1d]))", "{{qa_stage}}")],
          12, 32, 12, 8, "QA stages skipped on a retry by checkpoint reuse (R2-033, XD-W2-05-02).", "short"),
], [var_query("tenant", "Tenant", "label_values(replicant_qa_runs_total, tenant)")], ["replicant", "qa"])

for d in (overview, job, qa):
    (HERE / f"{d['uid']}.json").write_text(json.dumps(d, indent=2) + "\n")
print("wrote", [d["uid"] for d in (overview, job, qa)])
