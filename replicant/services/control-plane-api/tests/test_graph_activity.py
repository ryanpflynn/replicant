"""Unit tests for the run-activity transcript parser (activity.py) and the job graph status view (graph_view.py)."""
import datetime as dt
import json

import zstandard

from control_plane_api import activity, graph_view
from control_plane_api.app import create_app, iter_routes
from control_plane_api.config import Settings
from control_plane_api.routes_graph import read_spool

TS = "2026-10-25T16:59:49.{:03d}Z"
JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJydW46cnVuX2FiYyJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


def _cli_events() -> list[dict]:
    """Shapes captured from a scripted-backend implementer run (Claude Code 2.1.310 stream-json)."""
    return [
        {"type": "system", "subtype": "ui_invalidate"},
        {"type": "system", "subtype": "init", "model": "anthropic/claude-sonnet-5-5", "claude_code_version": "2.1.310",
         "tools": ["Bash", "Edit", "Read"], "session_id": "s1", "permissionMode": "dontAsk"},
        {"type": "assistant", "timestamp": TS.format(369), "message": {
            "id": "msg_1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 9000, "output_tokens": 1},
            "content": [{"type": "text", "text": "I'll look at ApplyRefund in the ledger package."}]}},
        {"type": "assistant", "timestamp": TS.format(372), "message": {
            "id": "msg_1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 9000, "output_tokens": 1},
            "content": [{"type": "tool_use", "id": "toolu_read", "name": "Read",
                         "input": {"file_path": "/workspace/repo/internal/ledger/ledger.go"}}]}},
        {"type": "user", "timestamp": TS.format(400), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_read", "content": "1\tpackage ledger\n" + "x\n" * 3000}]}},
        {"type": "assistant", "timestamp": TS.format(416), "message": {
            "id": "msg_2", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 11000, "output_tokens": 1},
            "content": [{"type": "tool_use", "id": "toolu_edit", "name": "Edit", "input": {
                "file_path": "/workspace/repo/internal/ledger/ledger.go", "old_string": "\t\tfxReversal := c.FXFeeMinor\n",
                "new_string": "\t\tfxReversal := Prorate(c.FXFeeMinor, refundMinor, c.AmountMinor)\n"}}]}},
        {"type": "user", "timestamp": TS.format(437), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_edit", "content": "The file has been updated successfully."}]}},
        {"type": "assistant", "timestamp": TS.format(450), "message": {
            "id": "msg_3", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 11500, "output_tokens": 1},
            "content": [{"type": "tool_use", "id": "toolu_bash", "name": "Bash", "input": {
                "command": f"curl -H 'Authorization: Bearer {JWT}' http://x && go test ./...",
                "description": "Run the unit tests"}}]}},
        {"type": "user", "timestamp": TS.format(990), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_bash", "is_error": True, "content": "FAIL ledger"}]},
         "tool_use_result": {"stdout": "FAIL ledger", "stderr": "exit status 1"}},
        {"type": "result", "subtype": "success", "num_turns": 5, "total_cost_usd": 0.0913, "duration_ms": 12754,
         "usage": {"input_tokens": 43500, "output_tokens": 430}, "is_error": False},
    ]


def _transcript() -> list[dict]:
    return [
        {"schema": "replicant.transcript/v1", "kind": "user_message", "seq": 1, "ts": TS.format(82),
         "input_preview": "Task: Refund ledger entries double-count FX fees"},
        {"schema": "replicant.transcript/v1", "kind": "assistant_text", "seq": 3, "ts": TS.format(371),
         "input_preview": "I'll look at ApplyRefund in the ledger package."},
        {"schema": "replicant.transcript/v1", "kind": "steering", "seq": 9, "ts": TS.format(500),
         "input_preview": "Use the existing Prorate helper"},
    ]


def test_feed_from_cli_events_pairs_results_diffs_and_redacts():
    feed = activity.build_feed(_cli_events(), _transcript(), [b"[bootstrap go-mod-download exit 0]\nok\n"])
    kinds = [i["kind"] for i in feed["items"]]
    assert kinds[:2] == ["user_message", "session_start"]  # the prompt precedes init; init time is backfilled
    assert kinds.count("user_message") == 1 and kinds.count("steering") == 1  # from the transcript only
    assert kinds.count("assistant_text") == 1                                 # cli_events wins, no duplicate
    calls = [i for i in feed["items"] if i["kind"] == "tool_call"]
    assert [c["tool"] for c in calls] == ["Read", "Edit", "Bash"]
    read, edit, bash = calls
    assert read["path"] == "internal/ledger/ledger.go"
    assert read["result"]["truncated"] and len(read["result"]["text"]) < 4200
    assert "-\t\tfxReversal := c.FXFeeMinor" in edit["diff"] and "+\t\tfxReversal := Prorate(" in edit["diff"]
    assert edit["diff"].startswith("--- a/internal/ledger/ledger.go")
    assert feed["items"][1]["at"] == TS.format(369)
    assert JWT not in json.dumps(feed) and "go test ./..." in bash["command"]
    assert bash["result"]["is_error"] and "exit status 1" in bash["result"]["text"]
    assert edit["usage"]["input_tokens"] == 11000 and "usage" not in read  # msg_1 usage counted once (on the text)
    res = [i for i in feed["items"] if i["kind"] == "result"][0]
    assert res["cost_usd"] == 0.0913 and res["num_turns"] == 5
    assert feed["tools"] == {"Read": 1, "Edit": 1, "Bash": 1}
    assert [entry["text"] for entry in feed["log"]] == ["[bootstrap go-mod-download exit 0]", "ok"]
    assert [i["i"] for i in feed["items"]] == list(range(len(feed["items"])))


def test_feed_falls_back_to_transcript_records():
    recs = _transcript() + [
        {"kind": "tool_use", "tool": "Bash", "tool_use_id": "t1", "ts": TS.format(600),
         "input_preview": json.dumps({"command": "go vet ./..."})},
        {"kind": "tool_result", "tool_use_id": "t1", "is_error": False, "ts": TS.format(700)},
    ]
    feed = activity.build_feed([], recs, [])
    call = [i for i in feed["items"] if i["kind"] == "tool_call"][0]
    assert call["command"] == "go vet ./..." and call["result"]["is_error"] is False


def test_usage_rows_filters_run_and_hides_caller():
    ev = {"run_id": "run_a", "occurred_at": "2026-10-25T17:00:02Z", "served_model": "claude-sonnet-5-5",
          "status": "ok", "usage": {"input_tokens": 12000, "output_tokens": 200}, "cost": {"micros_usd": 39000},
          "caller": {"token_jti": "rtk_secret"}, "budget_after": {"run_spent_micros": 136950}}
    u = activity.usage_rows([ev, {**ev, "run_id": "run_b"}], "run_a")
    assert u["totals"] == {"input_tokens": 12000, "output_tokens": 200, "cache_read_tokens": 0,
                           "cache_write_tokens": 0, "cost_usd": 0.039, "requests": 1, "errors": 0}
    assert "rtk_secret" not in json.dumps(u) and u["requests"][0]["run_spent_usd"] == 0.13695


SPEC = {
    "entry": "context",
    "nodes": [{"key": "context", "type": "context_assembler", "phase": "context"},
              {"key": "plan", "type": "plan_run", "phase": "planning", "config": {"agent": "planner-claude"}},
              {"key": "plan_gate", "type": "human_gate", "phase": "plan_gate", "config": {"gateKind": "plan_approval"}},
              {"key": "implement", "type": "implement_run", "phase": "implementing", "maxVisits": 4,
               "config": {"agent": "implementer-claude"}},
              {"key": "qa", "type": "qa_run", "phase": "qa"},
              {"key": "publish_gate", "type": "human_gate", "phase": "publish_gate",
               "config": {"gateKind": "publish_approval"}}],
    "edges": [{"from": "context", "to": "plan"}, {"from": "plan", "to": "plan_gate"},
              {"from": "plan_gate", "default": "end:rejected_at_gate", "switch": [
                  {"to": "implement", "when": "nodes['plan_gate'].outcome in ['approve', 'not_required']"}]},
              {"from": "implement", "to": "qa"},
              {"from": "qa", "default": "implement", "switch": [
                  {"to": "publish_gate", "when": "nodes['qa'].decision == 'proceed'"}]}],
}


def _ev(seq, typ, key, summary, sec):
    return {"seq": seq, "type": typ, "at": dt.datetime(2026, 10, 25, 17, 0, sec, tzinfo=dt.UTC), "summary": summary,
            "refs": {"node_key": key}}


def test_graph_view_statuses_loops_and_gate_wait():
    tl = [_ev(1, "node.started", "context", "context started", 0), _ev(2, "node.completed", "context", "context: completed", 1),
          _ev(3, "node.started", "plan", "plan started", 1), _ev(4, "node.completed", "plan", "plan: completed", 10),
          _ev(5, "node.started", "plan_gate", "plan_gate started", 10),
          _ev(6, "node.completed", "plan_gate", "plan_gate: not_required", 10),
          _ev(7, "node.started", "implement", "implement started", 10), _ev(8, "node.completed", "implement", "implement: completed", 20),
          _ev(9, "node.started", "qa", "qa started", 20), _ev(10, "node.completed", "qa", "qa: implement", 30),
          _ev(11, "node.started", "implement", "implement started", 30), _ev(12, "node.completed", "implement", "implement: completed", 40),
          _ev(13, "node.started", "qa", "qa started", 40), _ev(14, "node.completed", "qa", "qa: proceed", 50),
          _ev(15, "node.started", "publish_gate", "publish_gate started", 50)]
    snap = {"status": "waiting_human", "cycle": 0, "current_node": {"node_key": "publish_gate"},
            "gates": [{"gate_id": "gate_1", "kind": "publish_approval", "state": "open"}]}
    runs = [{"run_id": "run_1", "node_key": "implement", "cycle": 0, "ordinal": 1, "phase": "Succeeded"},
            {"run_id": "run_2", "node_key": "implement", "cycle": 0, "ordinal": 2, "phase": "Succeeded"}]
    v = graph_view.build_view(SPEC, tl, snap, runs, now=dt.datetime(2026, 10, 25, 17, 1, tzinfo=dt.UTC))
    n = {x["key"]: x for x in v["nodes"]}
    assert n["context"]["status"] == "succeeded" and n["plan_gate"]["status"] == "skipped"
    assert n["implement"]["visits"] == 2 and n["implement"]["duration_ms"] == 20000
    assert [r["run_id"] for r in n["implement"]["runs"]] == ["run_1", "run_2"]
    assert n["publish_gate"]["status"] == "waiting" and n["publish_gate"]["current"]
    assert n["publish_gate"]["gates"][0]["gate_id"] == "gate_1"
    assert n["publish_gate"]["duration_ms"] == 10000
    e = {(x["from"], x["to"]): x for x in v["edges"]}
    assert e[("qa", "implement")]["taken"] == 1 and e[("implement", "qa")]["taken"] == 2
    assert e[("qa", "implement")]["kind"] == "default"
    assert e[("plan_gate", "implement")]["label"] == "approve | not_required"
    assert "taken" not in e[("plan_gate", "end:rejected_at_gate")]
    assert v["ends"] == ["end:rejected_at_gate"]


def test_graph_view_terminal_failure_and_skip():
    tl = [_ev(1, "node.completed", "context", "context skipped", 0),
          _ev(2, "node.started", "plan", "plan started", 0), _ev(3, "node.failed", "plan", "plan failed: AgentError", 5)]
    v = graph_view.build_view(SPEC, tl, {"status": "failed", "terminal": {"outcome": "node_failed"}}, [])
    n = {x["key"]: x for x in v["nodes"]}
    assert n["context"]["status"] == "skipped" and n["plan"]["status"] == "failed" and n["plan"]["reason"] == "AgentError"
    assert n["implement"]["status"] == "pending" and v["terminal"]["node"] == "end:node_failed"
    tl2 = [_ev(1, "node.started", "plan", "plan started", 0)]
    v2 = graph_view.build_view(SPEC, tl2, {"status": "cancelled"}, [])
    assert {x["key"]: x for x in v2["nodes"]}["plan"]["status"] == "cancelled"


class _FakeS3:
    def __init__(self, objs):
        self.objs = objs

    def list_objects_v2(self, Bucket, Prefix, **kw):  # noqa: N803
        return {"Contents": [{"Key": k, "Size": len(v), "ETag": str(hash(v)), "LastModified": dt.datetime(2026, 1, 1)}
                             for k, v in sorted(self.objs.items()) if k.startswith(Prefix)], "IsTruncated": False}

    def get_object(self, Bucket, Key):  # noqa: N803
        import io
        return {"Body": io.BytesIO(self.objs[Key])}


def test_read_spool_prefers_final_parts_over_live():
    z = zstandard.ZstdCompressor().compress
    pre = "tenants/t/jobs/job_x/runs/run_y/"
    s3 = _FakeS3({pre + "transcript/part-00001.ndjson.zst": z(b'{"kind":"a"}\n'),
                  pre + "transcript/live/part-00001.ndjson.zst": z(b'{"kind":"stale"}\n'),
                  pre + "transcript/live/part-00002.ndjson.zst": z(b'{"kind":"b"}\n'),
                  pre + "sandbox_log/part-00001.ndjson.zst": z(b"line\n")})
    out = read_spool(s3, "b", pre)
    assert out["transcript"] == [b'{"kind":"a"}\n', b'{"kind":"b"}\n'] and out["provisional"] == 1
    assert out["sandbox_log"] == [b"line\n"] and out["cli_events"] == []


def test_routes_registered():
    paths = {getattr(r, "path", "") for r in iter_routes(list(create_app(Settings()).routes))}
    assert {"/v1/jobs/{job_id}/graph", "/v1/jobs/{job_id}/runs/{run_id}/activity"} <= paths
