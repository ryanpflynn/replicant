import { describe, expect, it } from "vitest";
import { badgeCount, groupWaiting, prIdle, titleWithCount, WAITING_UNAVAILABLE, waitingSummary } from "./waiting";
import type { WaitingGate, WaitingPr } from "./types";

const NOW = Date.parse("2026-10-25T12:00:00Z");
const iso = (minAgo: number) => new Date(NOW - minAgo * 60_000).toISOString();

const gate = (id: string, expiresInH: number | null, openedMinAgo = 30): WaitingGate => ({
  kind: "gate", gate_id: id, job_id: "job_" + id, tenant_id: "payments", gate_kind: "publish_approval", title: id,
  repo: "git.replicant.localhost:3300/acme-payments/ledger-api", risk_level: "low", requester: "usr_a",
  opened_at: iso(openedMinAgo), expires_at: expiresInH == null ? null : new Date(NOW + expiresInH * 3_600_000).toISOString(),
});
const pr = (n: number, waitingMinAgo: number, extra: Partial<WaitingPr> = {}): WaitingPr => ({
  kind: "pr", job_id: "job_" + n, tenant_id: "payments", title: "pr " + n, repo: "acme-payments/ledger-api",
  requester: "usr_a", role: "requester", pr: { number: n, state: "open" }, waiting_since: iso(waitingMinAgo), ...extra,
});

describe("waiting", () => {
  it("groups by kind, orders gates by expiry (none last) and PRs oldest-waiting first", () => {
    const w = groupWaiting({
      items: [pr(9, 10), gate("g_none", null), gate("g_late", 40), pr(8, 300), gate("g_soon", 2),
        { kind: "release_signoff" } as never],
      counts: { gates: 3, prs: 2, total: 5 }, as_of: iso(0),
    });
    expect(w.available).toBe(true);
    expect(w.gates.map((g) => g.gate_id)).toEqual(["g_soon", "g_late", "g_none"]);
    expect(w.prs.map((p) => p.pr.number)).toEqual([8, 9]);
    expect(w.total).toBe(5);
  });

  it("falls back to counting items when counts is missing", () => {
    const w = groupWaiting({ items: [gate("a", 1), pr(1, 5)] });
    expect(w.total).toBe(2);
    expect(groupWaiting({ items: undefined as never }).total).toBe(0);
  });

  it("hides the badge when unavailable, unknown or zero", () => {
    expect(badgeCount(undefined)).toBeNull();
    expect(badgeCount(WAITING_UNAVAILABLE)).toBeNull();
    expect(badgeCount(groupWaiting({ items: [], counts: { gates: 0, prs: 0, total: 0 } }))).toBeNull();
    expect(badgeCount(groupWaiting({ items: [gate("a", 1)], counts: { gates: 1, prs: 0, total: 1 } }))).toBe(1);
    expect(titleWithCount(2)).toBe("(2) replicant");
    expect(titleWithCount(null)).toBe("replicant");
  });

  it("labels PR idle time from idle_seconds, else from waiting_since", () => {
    expect(prIdle(pr(1, 300, { idle_seconds: 4210 }), NOW)).toBe("1 h");
    expect(prIdle(pr(1, 300), NOW)).toBe("5 h");
    expect(prIdle(pr(1, 3 * 24 * 60, { idle_seconds: null }), NOW)).toBe("3 d");
  });

  it("summarises for the top bar", () => {
    expect(waitingSummary(groupWaiting({ items: [gate("a", 1), pr(1, 5), pr(2, 6)] }))).toBe("1 approval · 2 pull requests");
    expect(waitingSummary(groupWaiting({ items: [] }))).toBe("nothing right now");
    expect(waitingSummary(WAITING_UNAVAILABLE)).toBe("not available from this API");
  });
});
