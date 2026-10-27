import { describe, expect, it } from "vitest";
import { headline, milestones, simpleSteps, simpleStepsFromGraph, stageStates } from "./simple";
import type { GraphNode } from "./graph";
import type { JobSnapshot, TimelineEvent } from "./types";

let seq = 0;
const ev = (type: string, summary: string, refs?: Record<string, string>): TimelineEvent =>
  ({ seq: ++seq, type, summary, at: `2026-10-27T10:${String(seq).padStart(2, "0")}:00Z`, refs });
const node = (n: string, res?: string) => (res === undefined
  ? [ev("node.started", `${n} started`, { node_key: n })]
  : [ev("node.started", `${n} started`, { node_key: n }), ev("node.completed", `${n}: ${res}`, { node_key: n })]);

const snap = (over: Partial<JobSnapshot> = {}): JobSnapshot => ({
  job_id: "job_x", tenant_id: "payments", title: "t", status: "running", phase: "implement", workflow_type: "bugfix",
  requester: "usr_a", seq: 1, as_of: "", cycle: 0, paused: false, ...over,
});

const upToImplement = () => [ev("job.routed", "routed to bugfix (rule)"), ...node("context", "completed"), ...node("plan", "completed"),
  ...node("risk_plan", "low"), ...node("plan_gate", "not_required")];
const full = () => [...upToImplement(), ...node("implement", "completed"), ...node("qa", "proceed"), ...node("review", "approve"),
  ...node("verify", "passed"), ...node("risk_diff", "low"), ev("node.started", "publish_gate started", { node_key: "publish_gate" }),
  ev("gate.opened", "publish_approval gate opened", { gate_id: "gate_1" }), ev("gate.decided", "publish_approval approve via ui", { gate_id: "gate_1" }),
  ev("node.completed", "publish_gate: approve", { node_key: "publish_gate" }), ...node("base_check", "current"),
  ev("node.started", "publish started", { node_key: "publish" }), ev("pr.opened", "PR #2 opened", { node_key: "publish" }),
  ev("node.completed", "publish: published", { node_key: "publish" }), ev("node.started", "pr_wait started", { node_key: "pr_wait" }),
  ev("pr.event", "PR event pull_request_merged"), ev("node.completed", "pr_wait: merged", { node_key: "pr_wait" }),
  ev("job.terminal", "job succeeded (pr_merged)")];

const states = (tl: TimelineEvent[], s: JobSnapshot) => simpleSteps(stageStates(tl, s), s).map((x) => x.st);

describe("simple view steps", () => {
  it("writing the fix", () => {
    const s = snap();
    const tl = [...upToImplement(), ...node("implement")];
    expect(states(tl, s)).toEqual(["done", "done", "now", "", "", ""]);
    expect(headline(s, simpleSteps(stageStates(tl, s), s))).toBe("Claude is writing the fix.");
  });
  it("between two nodes the next step is current", () => {
    const s = snap();
    const tl = [...upToImplement(), ...node("implement", "completed")];
    expect(states(tl, s)).toEqual(["done", "done", "done", "now", "", ""]);
    expect(headline(s, simpleSteps(stageStates(tl, s), s))).toMatch(/checking the change/);
  });
  it("waiting at the publish gate names the approvers", () => {
    const s = snap({ status: "waiting_human", gates: [{ gate_id: "gate_1", kind: "publish_approval", state: "open",
      eligible: { okta_groups: ["payments-approvers"] } }] });
    const tl = full().slice(0, full().findIndex((e) => e.type === "gate.opened") + 1);
    expect(states(tl, s)).toEqual(["done", "done", "done", "done", "wait", ""]);
    expect(headline(s, simpleSteps(stageStates(tl, s), s))).toBe("Waiting for an approver (payments-approvers) to approve the change.");
  });
  it("PR ready and done", () => {
    const pr = { repo: "r", state: "open", number: 24, url: "http://x/pulls/24" };
    expect(headline(snap({ status: "waiting_external", pull_request: pr }), [])).toBe("PR #24 is ready for review.");
    const s = snap({ status: "succeeded", pull_request: { ...pr, state: "merged" }, terminal: { at: "", outcome: "pr_merged" } });
    expect(states(full(), s)).toEqual(["done", "done", "done", "done", "done", "done"]);
    expect(headline(s, [])).toBe("Done: PR #24 was merged.");
    const closed = snap({ status: "rejected", pull_request: { ...pr, state: "closed" }, terminal: { at: "", outcome: "pr_closed_unmerged" } });
    expect(headline(closed, [])).toBe("Finished: PR #24 was closed without merging.");
    expect(headline(snap({ status: "succeeded", terminal: { at: "", outcome: "no_change_needed" } }), [])).toBe("Done: no changes were needed.");
    expect(headline(snap({ status: "rejected", terminal: { at: "", outcome: "rejected_at_gate" } }), [])).toBe("Stopped: an approver rejected the change.");
  });
  it("plan gate not required is not shown as a stop; failure marks the step", () => {
    const s = snap({ status: "failed", terminal: { at: "", outcome: "failed" } });
    const tl = [...upToImplement(), ...node("implement", "completed"), ...node("qa")];
    const st = simpleSteps(stageStates(tl, s), s);
    expect(st.map((x) => x.st)).toEqual(["done", "done", "done", "fail", "", ""]);
    expect(headline(s, st)).toBe("Something went wrong while checking; nothing more will happen.");
  });
  it("queued job: Requested is current", () => {
    const s = snap({ status: "queued" });
    expect(states([], s)).toEqual(["now", "", "", "", "", ""]);
    expect(headline(s, simpleSteps(stageStates([], s), s))).toBe("Queued: Claude will start shortly.");
  });
});

describe("milestones", () => {
  it("keeps only plain-language events", () => {
    const s = snap({ status: "succeeded", terminal: { at: "", outcome: "pr_merged" } });
    expect(milestones(full(), s).map((m) => m.text)).toEqual([
      "Request received", "Plan written", "Change written", "Tests passed", "Review passed", "Final checks passed",
      "Sent for approval", "Approved", "Pull request #2 opened", "Pull request merged", "Finished: PR merged"]);
  });
  it("problems and rejections read plainly", () => {
    const s = snap({ status: "rejected", terminal: { at: "", outcome: "rejected" } });
    const tl = [ev("node.completed", "qa: repair", { node_key: "qa" }), ev("gate.decided", "plan_approval reject via slack", { gate_id: "g" }),
      ev("job.terminal", "job rejected (rejected)")];
    expect(milestones(tl, s).map((m) => [m.text, m.family])).toEqual([
      ["Tests found problems", "warning"], ["Rejected", "danger"], ["Stopped: rejected at a gate", "danger"]]);
  });
});

describe("steps from the job's graph", () => {
  const n = (phase: string, status: GraphNode["status"] = "pending") => ({ phase, status });
  const run = { status: "running" } as Pick<JobSnapshot, "status">;

  it("hides steps the workflow has no node for (qa_only)", () => {
    const st = simpleStepsFromGraph([n("context", "succeeded"), n("qa", "running")], run, "qa_only");
    expect(st.map((x) => x.label)).toEqual(["Requested", "Running the tests"]);
    expect(st.map((x) => x.st)).toEqual(["done", "now"]);
  });

  it("hotfix has no planning step and checks without the planning label", () => {
    const st = simpleStepsFromGraph([n("context", "succeeded"), n("implementing", "running"), n("qa"), n("review"),
      n("verify"), n("publish_gate"), n("publishing"), n("pr_review")], run, "hotfix");
    expect(st.map((x) => x.key)).toEqual(["requested", "change", "checking", "approval", "pr"]);
    expect(st[1].st).toBe("now");
  });

  it("pr_repair makes its change in the repair phase and updates the PR", () => {
    const st = simpleStepsFromGraph([n("context", "succeeded"), n("repairing", "succeeded"), n("qa", "succeeded"),
      n("verify", "succeeded"), n("publish_gate", "waiting"), n("publishing"), n("memory")],
    { status: "waiting_human" }, "pr_repair");
    expect(st.map((x) => x.label)).toEqual(["Requested", "Making the fixes", "Checking (tests)", "Approval",
      "Updating the pull request"]);
    expect(st.map((x) => x.st)).toEqual(["done", "done", "done", "wait", ""]);
  });

  it("an unentered step before a reached one is shown as not needed", () => {
    const st = simpleStepsFromGraph([n("context", "succeeded"), n("plan_gate"), n("implementing", "running")], run, "bugfix");
    expect(st.map((x) => x.st)).toEqual(["done", "skip", "now"]);
  });

  it("a failed node marks its step failed", () => {
    const st = simpleStepsFromGraph([n("context", "succeeded"), n("implementing", "succeeded"), n("qa", "failed")],
      { status: "failed" }, "feature");
    expect(st.find((x) => x.key === "checking")?.st).toBe("fail");
  });

  it("milestones read node types, not node keys", () => {
    const tl = [{ seq: 1, at: "t", type: "node.completed", summary: "fix_it: completed", refs: { node_key: "fix_it" } }] as TimelineEvent[];
    expect(milestones(tl, { status: "running" } as JobSnapshot, new Map([["fix_it", "implement_run"]]))[0].text)
      .toBe("Change written");
  });
});
