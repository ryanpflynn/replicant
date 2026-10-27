// Status vocabulary from design/tokens.json#status: shape carries meaning, colour reinforces it.
export type Family = "info" | "success" | "danger" | "special" | "neutral" | "attention" | "warning";
export interface StatusStyle { family: Family; glyph: string; label: string }

const JOB: Record<string, StatusStyle> = {
  queued: { family: "neutral", glyph: "ring-dotted", label: "Queued" },
  running: { family: "info", glyph: "ring-spin", label: "Running" },
  waiting_human: { family: "attention", glyph: "diamond", label: "Waiting on human" },
  waiting_external: { family: "neutral", glyph: "ring-half", label: "In PR review" },
  paused: { family: "neutral", glyph: "bars", label: "Paused" },
  succeeded: { family: "success", glyph: "check", label: "Succeeded" },
  failed: { family: "danger", glyph: "cross", label: "Failed" },
  cancelled: { family: "neutral", glyph: "slash", label: "Cancelled" },
  rejected: { family: "danger", glyph: "slash", label: "Rejected" },
  expired: { family: "neutral", glyph: "hourglass", label: "Expired" },
  taken_over: { family: "special", glyph: "hand", label: "Taken over" },
};

const GATE: Record<string, StatusStyle> = {
  open: { family: "attention", glyph: "diamond", label: "Open" },
  partially_approved: { family: "attention", glyph: "diamond", label: "Partially approved" },
  approved: { family: "success", glyph: "check", label: "Approved" },
  rejected: { family: "danger", glyph: "cross", label: "Rejected" },
  changes_requested: { family: "special", glyph: "return", label: "Changes requested" },
  answered: { family: "success", glyph: "check", label: "Answered" },
  expired: { family: "neutral", glyph: "hourglass", label: "Expired" },
  cancelled: { family: "neutral", glyph: "slash", label: "Cancelled" },
};

const RUN: Record<string, StatusStyle> = {
  Pending: { family: "neutral", glyph: "ring-dotted", label: "Pending" },
  Provisioning: { family: "info", glyph: "ring-dotted", label: "Provisioning" },
  Running: { family: "info", glyph: "ring-spin", label: "Running" },
  Finalizing: { family: "info", glyph: "ring-half", label: "Finalizing" },
  Cancelling: { family: "neutral", glyph: "slash", label: "Cancelling" },
  Succeeded: { family: "success", glyph: "check", label: "Succeeded" },
  Failed: { family: "danger", glyph: "cross", label: "Failed" },
  Cancelled: { family: "neutral", glyph: "slash", label: "Cancelled" },
};

const BUNDLE: Record<string, StatusStyle> = {
  built: { family: "neutral", glyph: "dot-hollow", label: "built" },
  evaluating: { family: "info", glyph: "ring-spin", label: "evaluating" },
  eval_passed: { family: "success", glyph: "check", label: "eval passed" },
  eval_failed: { family: "danger", glyph: "cross", label: "eval failed" },
  shadowing: { family: "info", glyph: "ring-half", label: "shadowing" },
  canary: { family: "info", glyph: "ring-spin", label: "canary" },
  promoted: { family: "success", glyph: "dot", label: "promoted" },
  rolled_back: { family: "danger", glyph: "return", label: "rolled back" },
  withdrawn: { family: "neutral", glyph: "slash", label: "withdrawn" },
  deprecated: { family: "neutral", glyph: "dot-hollow", label: "deprecated" },
  revoked: { family: "danger", glyph: "slash", label: "revoked" },
};

const fallback = (s: string): StatusStyle => ({ family: "neutral", glyph: "dot-hollow", label: s.replace(/_/g, " ") });

export const jobStatus = (s: string) => JOB[s] ?? fallback(s);
export const gateStatus = (s: string) => GATE[s] ?? fallback(s);
export const runStatus = (s: string) => RUN[s] ?? fallback(s);
export const bundleStatus = (s: string) => BUNDLE[s] ?? fallback(s);

export const TERMINAL = new Set(["succeeded", "failed", "cancelled", "rejected", "expired", "taken_over"]);
export const OPEN_STATUSES = ["queued", "running", "waiting_human", "waiting_external", "paused"];

export const GATE_KIND_SHORT: Record<string, string> = {
  plan_approval: "Plan", publish_approval: "Publish", clarification: "Question", budget_extension: "Budget",
  bundle_migration: "Migration", leak_purge: "Leak purge",
};

/** Terminal outcome in words (JobSnapshot.terminal.outcome). */
export const OUTCOME_WORDS: Record<string, string> = {
  pr_merged: "PR merged", pr_closed: "PR closed without merge", no_changes: "no changes needed",
  cancelled: "cancelled", rejected: "rejected at a gate", expired: "expired at a gate",
};
