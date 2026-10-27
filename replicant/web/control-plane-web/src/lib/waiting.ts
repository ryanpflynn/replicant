// "Waiting on you" (GET /v1/me/waiting): pure helpers for grouping, ordering, ages and the badge.
import { age, parseTs } from "./format";
import type { WaitingGate, WaitingPr, WaitingResponse } from "./types";

export interface Waiting {
  /** false when control-plane-api predates /v1/me/waiting (404): the screen says so and the badge is hidden. */
  available: boolean;
  gates: WaitingGate[];
  prs: WaitingPr[];
  total: number;
  asOf?: string;
}

export const WAITING_UNAVAILABLE: Waiting = { available: false, gates: [], prs: [], total: 0 };

const ts = (s?: string | null, missing = Infinity) => parseTs(s) ?? missing;

/** Split by kind and order like the API promises (gates by expiry, then PRs oldest-waiting first), so the UI
 * does not depend on the server's order. Unknown kinds from a newer API are ignored. */
export function groupWaiting(r: WaitingResponse): Waiting {
  const items = Array.isArray(r?.items) ? r.items : [];
  const gates = items.filter((i): i is WaitingGate => i?.kind === "gate")
    .sort((a, b) => ts(a.expires_at) - ts(b.expires_at) || ts(a.opened_at) - ts(b.opened_at));
  const prs = items.filter((i): i is WaitingPr => i?.kind === "pr")
    .sort((a, b) => ts(a.waiting_since) - ts(b.waiting_since) || a.pr.number - b.pr.number);
  const total = typeof r?.counts?.total === "number" ? r.counts.total : gates.length + prs.length;
  return { available: true, gates, prs, total, asOf: r?.as_of };
}

/** Rail badge value: null hides it (unknown, unavailable, or nothing waiting). */
export function badgeCount(w?: Waiting): number | null {
  if (!w || !w.available || w.total <= 0) return null;
  return w.total;
}

/** Browser tab title: "(2) replicant". */
export const titleWithCount = (n: number | null, base = "replicant") => (n ? `(${n}) ${base}` : base);

/** How long the PR has been idle: from idle_seconds when the API sends it, else since waiting_since. */
export function prIdle(p: WaitingPr, now = Date.now()): string {
  if (typeof p.idle_seconds === "number" && p.idle_seconds >= 0) return age(new Date(now - p.idle_seconds * 1000).toISOString(), now);
  return age(p.waiting_since, now);
}

export const ROLE_TEXT: Record<WaitingPr["role"], string> = {
  requester: "You requested this job; the PR needs a reviewer to merge it.",
  reviewer: "You approved this job's publish gate, so you reviewed this exact diff.",
};

/** "TopBar" summary: "2 approvals · 1 pull request". */
export function waitingSummary(w: Waiting): string {
  if (!w.available) return "not available from this API";
  if (w.gates.length + w.prs.length === 0) return "nothing right now";
  const parts: string[] = [];
  if (w.gates.length) parts.push(`${w.gates.length} approval${w.gates.length === 1 ? "" : "s"}`);
  if (w.prs.length) parts.push(`${w.prs.length} pull request${w.prs.length === 1 ? "" : "s"}`);
  return parts.join(" · ");
}
