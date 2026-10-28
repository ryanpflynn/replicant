"""History budget model for a 30-day job (history-budget.md).

Calibrated by `tests/test_scenarios.py history_budget` (measured per-run event counts on Temporal
server 1.32.0) and extended with costs that the test does not exercise: production hint Signals (one
per AgentRun phase transition instead of the fake runner's one), gate reminders and escalations during
multi-day waits, and PR-wait reminders. Usage:

    python tools/history_model.py measured.json   # measured.json = --dump output with history_budget
"""

from __future__ import annotations

import json
import sys

# Cost of one extra event of each kind, including the workflow task it causes (3 events). These are
# structural, not measured: Temporal writes TimerStarted+TimerFired, Signaled, ActivityTaskScheduled/
# Started/Completed, and WorkflowTaskScheduled/Started/Completed per task (docs.temporal.io/references/events).
WFT = 3
COST_REMINDER = (2 + WFT) + (3 + WFT) + (1 + 3)  # timer, notify activity, projection emit = 15
COST_EXTRA_HINT = (1 + WFT) + (3 + WFT) + 1  # Signal, get_run poll, cancelled poll timer = 11
COST_POLL = (2 + WFT) + (3 + WFT)  # one timer-driven poll = 11


def model(measured: dict) -> dict:
    runs = [r["events"] for r in measured["runs_detail"]]
    initial, final = runs[0], runs[-1]
    cycle_runs = runs[2:-1:2]  # repair-cycle runs alternate with pr_wait runs
    wait_runs = runs[1:-1:2]
    s_cycle = max(cycle_runs)
    s_wait = max(wait_runs)
    runner_runs = measured["runner_runs"]
    # 30-day timeline assumptions (documented in history-budget.md section 2)
    plan_gate_reminders = 3 + 1  # waits 2 days: reminders at 4 h, 24 h, 48 h + escalation at 24 h
    publish_gate_reminders = 2 + 1  # waits 1 day
    _push_gate_reminders = 5 * 1  # (documented, not used in the totals) each repair push waits about 4 h: one reminder
    pr_reminders = 2  # day 7 and day 21 without activity, once over the job
    extra_hints = 3 * runner_runs  # notifier: Provisioning, Running, Finalizing, Succeeded (fake sent 1)
    added_initial = (plan_gate_reminders + publish_gate_reminders) * COST_REMINDER + 3 * 5 * COST_EXTRA_HINT
    per_cycle_added = 1 * COST_REMINDER + 3 * 2 * COST_EXTRA_HINT  # repair + qa run per cycle
    total = (initial + added_initial
             + sum(cycle_runs) + len(cycle_runs) * per_cycle_added
             + sum(wait_runs) + pr_reminders * COST_REMINDER + final)
    worst_node = (1800 // 15 + (28800 + 1800 + 300) // 120) * COST_POLL  # 8 h active run, 30 min startup
    return {
        "measured_runs": len(runs), "measured_total": sum(runs), "measured_max_run": max(runs),
        "initial_run_modeled": initial + added_initial, "cycle_run_modeled": s_cycle + per_cycle_added,
        "pr_wait_run": s_wait, "job_total_modeled": total, "runs": len(runs),
        "worst_single_node_poll_events": worst_node, "extra_hints_counted": extra_hints,
        "bytes_total_measured": sum(r["bytes"] for r in measured["runs_detail"]),
        "bytes_max_run_measured": max(r["bytes"] for r in measured["runs_detail"]),
        "carry_bytes_max_measured": max(r["input_bytes"] for r in measured["runs_detail"]),
    }


def actions(measured: dict) -> int:
    """Billable Temporal Cloud actions in the measured history (docs.temporal.io/cloud/actions): starts and
    Continue-As-New, activity schedules, timers, Signals, accepted Updates, and search attribute upserts."""
    t: dict[str, int] = {}
    for r in measured["runs_detail"]:
        for k, v in r["types"].items():
            t[k] = t.get(k, 0) + v
    keys = ["workflow_execution_started_event_attributes", "activity_task_scheduled_event_attributes",
            "timer_started_event_attributes", "workflow_execution_signaled_event_attributes",
            "workflow_execution_update_accepted_event_attributes", "upsert_workflow_search_attributes_event_attributes"]
    return sum(t.get(k, 0) for k in keys)


if __name__ == "__main__":
    d = json.load(open(sys.argv[1]))["results"]["history_budget"]
    out = model(d)
    out["billable_actions_measured"] = actions(d)
    print(json.dumps(out, indent=2))
