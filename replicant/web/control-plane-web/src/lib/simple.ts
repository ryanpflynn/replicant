// The job phase track (STAGES / stageStates, shared with the Detailed view) and the plain-language Simple view built
// on it (WEB-UIPREF): six steps, a one-sentence headline and milestone-only timeline. Pure functions, unit-tested.
import { OUTCOME_WORDS, TERMINAL, type Family } from "./status";
import type { GraphNode } from "./graph";
import type { GateView, JobSnapshot, TimelineEvent } from "./types";

// Phase track for the bugfix graph (ui/job-detail.md (2)): stage → node keys of replicant-worker's ReplicantJob.
export const STAGES: Array<{ label: string; nodes: string[]; gate?: boolean }> = [
  { label: "routing", nodes: ["__routed"] },
  { label: "context", nodes: ["context"] },
  { label: "planning", nodes: ["plan", "risk_plan"] },
  { label: "plan gate", nodes: ["plan_gate"], gate: true },
  { label: "implementing", nodes: ["implement", "repair"] },
  { label: "qa", nodes: ["qa"] },
  { label: "review", nodes: ["review"] },
  { label: "verify", nodes: ["verify", "risk_diff"] },
  { label: "publish gate", nodes: ["publish_gate"], gate: true },
  { label: "publishing", nodes: ["base_check", "publish"] },
  { label: "pr review", nodes: ["pr_wait"] },
];

export type StageState = { label: string; gate?: boolean; st: string; title?: string };

export function stageStates(tl: TimelineEvent[], s: JobSnapshot): StageState[] {
  const started = new Set<string>();
  const done = new Map<string, string>();
  for (const e of tl) {
    const n = e.refs?.node_key;
    if (e.type === "job.routed") { started.add("__routed"); done.set("__routed", "routed"); }
    if (n && e.type === "node.started") started.add(n);
    if (n && e.type === "node.completed") done.set(n, e.summary.split(": ").slice(1).join(": "));
  }
  const terminal = TERMINAL.has(s.status);
  return STAGES.map((g) => {
    const anyStarted = g.nodes.some((n) => started.has(n));
    const allDone = anyStarted && g.nodes.filter((n) => started.has(n)).every((n) => done.has(n));
    const outcome = g.nodes.map((n) => done.get(n)).filter(Boolean).join(", ");
    let st = "";
    if (allDone) st = "done";
    else if (anyStarted) st = terminal ? (s.status === "succeeded" ? "done" : s.status === "failed" ? "fail" : "skip") : (g.gate && s.status === "waiting_human" ? "wait" : "now");
    if (g.label === "pr review" && s.status === "succeeded") st = "done";
    if (allDone && g.gate && /not_required/.test(outcome)) st = "skip";
    return { label: g.label, gate: g.gate, st, title: outcome || undefined };
  });
}

// ---- Simple view ----
/** The six plain steps and the STAGES they are made of. */
export const SIMPLE_STEPS: Array<{ key: string; label: string; stages: string[] }> = [
  { key: "requested", label: "Requested", stages: ["routing", "context"] },
  { key: "planning", label: "Planning", stages: ["planning", "plan gate"] },
  { key: "change", label: "Making the change", stages: ["implementing"] },
  { key: "checking", label: "Checking (tests and review)", stages: ["qa", "review", "verify"] },
  { key: "approval", label: "Approval", stages: ["publish gate"] },
  { key: "pr", label: "Pull request", stages: ["publishing", "pr review"] },
];

// ---- Steps from the job's own graph (any workflow) ----
// Every workflow graph (C-DEF GraphDefinition) tags its nodes with a phase from one vocabulary, so the plain steps
// are derived from the job's graph: each phase belongs to one step, and steps the graph has no node for are hidden.
const PHASE_STEP: Record<string, string> = {
  routing: "requested", context: "requested", planning: "planning", plan_gate: "planning", implementing: "change",
  qa: "checking", review: "checking", verify: "checking", publish_gate: "approval", rebase_reverify: "pr",
  publishing: "pr", pr_review: "pr", repairing: "pr", memory: "",
};
const STEP_ORDER = ["requested", "planning", "change", "checking", "approval", "pr"];

function stepOf(phase: string | undefined, phases: Set<string>): string {
  // A graph without an implementing phase (pr_repair) makes its change in the repair phase.
  if (phase === "repairing" && !phases.has("implementing")) return "change";
  return PHASE_STEP[phase ?? ""] ?? "";
}

function stepLabel(key: string, phases: Set<string>, workflow: string): string {
  switch (key) {
    case "requested": return "Requested";
    case "planning": return "Planning";
    case "change": return phases.has("implementing") ? "Making the change" : "Making the fixes";
    case "checking":
      if (phases.has("review")) return "Checking (tests and review)";
      return workflow === "qa_only" ? "Running the tests" : "Checking (tests)";
    case "approval": return "Approval";
    default: return phases.has("pr_review") ? "Pull request" : "Updating the pull request";
  }
}

/** The plain steps of this job's workflow and their state, from the graph view's node statuses. */
export function simpleStepsFromGraph(nodes: Pick<GraphNode, "phase" | "status">[], s: Pick<JobSnapshot, "status">,
                                     workflow = ""): SimpleStep[] {
  const phases = new Set(nodes.map((n) => n.phase ?? ""));
  const byStep = new Map<string, NodeStatusLite[]>();
  for (const n of nodes) {
    const k = stepOf(n.phase, phases);
    if (k) byStep.set(k, [...(byStep.get(k) ?? []), n.status]);
  }
  const terminal = TERMINAL.has(s.status);
  const steps: SimpleStep[] = STEP_ORDER.filter((k) => byStep.has(k)).map((k) => {
    const sts = byStep.get(k)!;
    let st: StepState = "";
    if (sts.includes("failed")) st = "fail";
    else if (sts.includes("waiting")) st = "wait";
    else if (sts.includes("running")) st = "now";
    else if (sts.includes("succeeded")) st = "done";
    else if (sts.every((x) => x === "skipped")) st = "skip";
    else if (sts.includes("skipped") || sts.includes("cancelled")) st = "skip";
    return { key: k, label: stepLabel(k, phases, workflow), st };
  });
  // A step nobody entered, before one that was reached, was not needed on this path (e.g. a plan gate turned off).
  const last = steps.map((x) => x.st !== "").lastIndexOf(true);
  steps.forEach((x, i) => { if (x.st === "" && i < last) x.st = "skip"; });
  if (s.status === "succeeded") steps.forEach((x) => { if (x.st === "now") x.st = "done"; });
  // Between steps (a node finished, the next has not started yet): the first unreached step is the current one.
  if (!terminal && s.status !== "paused" && !steps.some((x) => x.st === "now" || x.st === "wait" || x.st === "fail")) {
    const next = steps.find((x) => x.st === "");
    if (next) next.st = "now";
  }
  return steps;
}

type NodeStatusLite = GraphNode["status"];

/** "" = not reached; done | now | wait (on a person) | fail | skip (not needed / stopped before it). */
export type StepState = "" | "done" | "now" | "wait" | "fail" | "skip";
export interface SimpleStep { key: string; label: string; st: StepState }

export function simpleSteps(track: StageState[], s: Pick<JobSnapshot, "status">): SimpleStep[] {
  const by = new Map(track.map((t) => [t.label, t.st]));
  const terminal = TERMINAL.has(s.status);
  const steps = SIMPLE_STEPS.map((step) => {
    const sts = step.stages.map((l) => by.get(l) ?? "");
    let st: StepState = "";
    if (sts.includes("fail")) st = "fail";
    else if (sts.includes("wait")) st = "wait";
    else if (sts.includes("now")) st = "now";
    else if (sts.every((x) => x === "skip")) st = "skip";
    else if (sts.every((x) => x === "done" || x === "skip" || (x === "" && terminal))) st = sts.includes("done") ? "done" : "";
    else if (sts.includes("done")) st = terminal ? "done" : "now";      // between two nodes of the same step
    else if (sts.includes("skip")) st = "skip";
    return { key: step.key, label: step.label, st };
  });
  // Between steps (a node finished, the next has not started yet): the first unreached step is the current one.
  if (!terminal && s.status !== "paused" && !steps.some((x) => x.st === "now" || x.st === "wait" || x.st === "fail")) {
    const next = steps.find((x) => x.st === "");
    if (next) next.st = "now";
  }
  return steps;
}

const GATE_ASK: Record<string, string> = {
  plan_approval: "to approve the plan", publish_approval: "to approve the change", budget_extension: "to approve more budget",
  bundle_migration: "to approve a migration",
};

const groups = (g?: GateView) => g?.eligible?.okta_groups?.length ? ` (${g.eligible.okta_groups.join(", ")})` : "";

/** One plain sentence for where the job is. */
export function headline(s: JobSnapshot, steps: SimpleStep[]): string {
  const pr = s.pull_request;
  const gate = (s.gates ?? []).find((g) => g.state === "open" || g.state === "partially_approved");
  switch (s.status) {
    case "succeeded": {
      const o = s.terminal?.outcome;
      if (o === "pr_merged") return pr ? `Done: PR #${pr.number} was merged.` : "Done: merged.";
      if (o === "no_change_needed" || o === "no_changes") return "Done: no changes were needed.";
      if (o === "repair_pushed") return pr ? `Done: the fixes were pushed to PR #${pr.number}.` : "Done: the fixes were pushed.";
      return o && OUTCOME_WORDS[o] ? `Done: ${OUTCOME_WORDS[o]}.` : "Done.";
    }
    case "failed": {
      const at = steps.find((x) => x.st === "fail");
      const why = s.terminal?.outcome && OUTCOME_WORDS[s.terminal.outcome] && s.terminal.outcome !== "node_failed"
        ? ` (${OUTCOME_WORDS[s.terminal.outcome]})` : "";
      return `Something went wrong${at ? ` while ${at.label.toLowerCase().replace(/ \(.*\)$/, "")}` : ""}${why}; nothing more will happen.`;
    }
    case "cancelled": return "Cancelled; nothing more will happen.";
    case "rejected":
      if (s.terminal?.outcome === "pr_closed_unmerged" || s.terminal?.outcome === "pr_closed")
        return pr ? `Finished: PR #${pr.number} was closed without merging.` : "Finished: the PR was closed without merging.";
      if (s.terminal?.outcome === "clarification_rejected") return "Stopped: the question to the requester was declined.";
      return "Stopped: an approver rejected the change.";
    case "expired":
      if (s.terminal?.outcome === "pr_wait_expired") return pr ? `Stopped: PR #${pr.number} waited too long for review.` : "Stopped: the PR waited too long for review.";
      if (s.terminal?.outcome === "pause_expired") return "Stopped: it stayed paused too long.";
      return "Stopped: nobody approved it in time.";
    case "taken_over": return "A person took this job over from Claude.";
    case "paused": return "Paused; nothing runs until someone resumes it.";
    case "queued": return "Queued: Claude will start shortly.";
    case "waiting_human":
      if (gate?.kind === "clarification") return "Waiting for someone to answer a question from Claude.";
      return `Waiting for an approver${groups(gate)}${gate ? ` ${GATE_ASK[gate.kind] ?? "to decide"}` : ""}.`;
    case "waiting_external":
      return pr ? `PR #${pr.number} is ready for review.` : "Waiting for the pull request to be reviewed.";
    default: break;
  }
  const now = steps.find((x) => x.st === "now")?.key;
  switch (now) {
    case "requested": return "Claude is reading the request.";
    case "planning": return s.workflow_type === "bugfix" ? "Claude is planning the fix." : "Claude is planning the change.";
    case "change":
      if (s.cycle > 0) return "Claude is fixing problems the checks found.";
      return s.workflow_type === "bugfix" ? "Claude is writing the fix." : "Claude is writing the change.";
    case "checking": {
      const label = steps.find((x) => x.key === "checking")?.label ?? "";
      return /review/.test(label) || !label ? "Claude is checking the change (tests and review)." : "Claude is running the tests.";
    }
    case "approval": return "Getting the change ready for approval.";
    case "pr": return pr ? `PR #${pr.number} is open.` : "Claude is opening the pull request.";
    default: return "Claude is working on it.";
  }
}

export interface Milestone { key: string; at: string; text: string; family: Family }

const COMPLETED: Record<string, (res: string) => [string, Family] | null> = {
  plan: () => ["Plan written", "neutral"],
  implement: () => ["Change written", "neutral"],
  repair: () => ["Fixes written", "neutral"],
  qa: (r) => (/proceed|pass/.test(r) ? ["Tests passed", "success"] : ["Tests found problems", "warning"]),
  review: (r) => (/approve/.test(r) ? ["Review passed", "success"] : ["Review asked for changes", "warning"]),
  verify: (r) => (/pass/.test(r) ? ["Final checks passed", "success"] : ["Final checks found problems", "warning"]),
};

const DECIDED: Record<string, [string, Family]> = {
  approve: ["Approved", "success"], reject: ["Rejected", "danger"], request_changes: ["Changes requested", "special"],
};

/** Only the plain-language events, oldest first, read from TimelineEvent.type and .summary. */
// Node type -> the COMPLETED entry (graphs name their nodes freely; the type says what the node does).
const TYPE_KEY: Record<string, string> = {
  plan_run: "plan", implement_run: "implement", repair_run: "repair", qa_run: "qa", review_run: "review", verifier: "verify",
};

export function milestones(tl: TimelineEvent[], s: Pick<JobSnapshot, "status" | "terminal">,
                           typeOf?: Map<string, string>): Milestone[] {
  const out: Milestone[] = [];
  for (const e of tl) {
    const k = `m${e.seq}`;
    const key = e.refs?.node_key ?? "";
    const n = (typeOf && TYPE_KEY[typeOf.get(key) ?? ""]) || key;
    if (e.type === "job.routed") out.push({ key: k, at: e.at, text: "Request received", family: "neutral" });
    else if (e.type === "node.completed") {
      const m = COMPLETED[n]?.(e.summary.split(": ").slice(1).join(": "));
      if (m) out.push({ key: k, at: e.at, text: m[0], family: m[1] });
    } else if (e.type === "gate.opened") {
      out.push({ key: k, at: e.at, text: /plan/.test(e.summary) ? "Plan sent for approval" : /clarification/.test(e.summary) ? "Question asked" : "Sent for approval", family: "attention" });
    } else if (e.type === "gate.decided") {
      const d = e.summary.split(" ")[1] ?? "";
      const [t, f] = DECIDED[d] ?? ["Decided", "neutral"];
      out.push({ key: k, at: e.at, text: t, family: f });
    } else if (e.type === "pr.opened") {
      const no = e.summary.match(/#(\d+)/)?.[1];
      out.push({ key: k, at: e.at, text: no ? `Pull request #${no} opened` : "Pull request opened", family: "neutral" });
    } else if (e.type === "pr.event" && /merged/.test(e.summary)) {
      out.push({ key: k, at: e.at, text: "Pull request merged", family: "success" });
    } else if (e.type === "pr.event" && /closed/.test(e.summary)) {
      out.push({ key: k, at: e.at, text: "Pull request closed", family: "neutral" });
    } else if (e.type === "job.terminal") {
      const o = s.terminal?.outcome;
      const word = o ? OUTCOME_WORDS[o] ?? o.replace(/_/g, " ") : s.status;
      out.push({ key: k, at: e.at, text: s.status === "succeeded" ? `Finished: ${word}` : `Stopped: ${word}`,
        family: s.status === "succeeded" ? "success" : s.status === "failed" || s.status === "rejected" ? "danger" : "neutral" });
    }
  }
  return out;
}
