// "Waiting on you" (/waiting) and the rail badge, both themes. Live by default: signs in as WAITING_USER (default
// alice) and checks the badge against the rows and against GET /v1/me/waiting. WAITING_MOCK=1 answers that endpoint
// from a fixture (route interception) so the screen can be shot without live data, and adds an "older API" (404) check.
import * as path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { apiGet, as, SHOTS, settle, shot } from "./helpers";

const USER = process.env.WAITING_USER ?? "alice";
const MOCK = process.env.WAITING_MOCK === "1";

interface WaitingBody { items: Array<{ kind: string }>; counts: { gates: number; prs: number; total: number }; as_of: string }

function fixture(pid: Record<string, string>): WaitingBody {
  const now = Date.now();
  const at = (min: number) => new Date(now + min * 60_000).toISOString();
  const repo = "git.replicant.localhost:3300/acme-payments/ledger-api";
  const items = [
    { kind: "gate", gate_id: "gate_01m5x0demo00000000000000a", job_id: "job_01m5x0demo00000000000000a", tenant_id: "payments",
      gate_kind: "publish_approval", title: "Prorate the FX fee on partial refunds", repo, risk_level: "medium",
      requester: pid.bob, opened_at: at(-95), expires_at: at(7 * 60), why: "eligible_approver", waiting_since: at(-95),
      href: "/approvals/gate_01m5x0demo00000000000000a" },
    { kind: "gate", gate_id: "gate_01m5x0demo00000000000000b", job_id: "job_01m5x0demo00000000000000b", tenant_id: "payments",
      gate_kind: "plan_approval", title: "Add an idempotency key to payout retries", repo: "git.replicant.localhost:3300/acme-payments/payouts-svc",
      risk_level: "low", requester: pid.erin, opened_at: at(-20), expires_at: at(3 * 24 * 60), why: "eligible_approver",
      waiting_since: at(-20), href: "/approvals/gate_01m5x0demo00000000000000b" },
    { kind: "pr", job_id: "job_01m5x0demo00000000000000c", tenant_id: "payments", title: "Round ledger totals with banker's rounding",
      repo, requester: pid.alice, role: "requester",
      pr: { number: 8, url: "http://127.0.0.1:3300/acme-payments/ledger-api/pulls/8", state: "open", opened_at: at(-26 * 60) },
      waiting_since: at(-26 * 60), idle_seconds: 4210, last_reminded_at: at(-60), href: "/jobs/job_01m5x0demo00000000000000c" },
    { kind: "pr", job_id: "job_01m5x0demo00000000000000d", tenant_id: "payments", title: "Fix broken links in the chargeback FAQ",
      repo, requester: pid.bob, role: "reviewer",
      pr: { number: 11, url: "http://gitea:3300/acme-payments/ledger-api/pulls/11", state: "open", opened_at: at(-50) },
      waiting_since: at(-50), idle_seconds: 600, last_reminded_at: null, href: "/jobs/job_01m5x0demo00000000000000d" },
  ];
  return { items, counts: { gates: 2, prs: 2, total: 4 }, as_of: at(0) };
}

/** Real principal IDs of the dev users, so the requester column resolves to names as it does live. */
async function principals(request: APIRequestContext): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const u of ["alice", "bob", "erin"]) out[u] = (await apiGet<{ principal_id: string }>(request, "/v1/me", u)).principal_id;
  return out;
}

async function mock(page: Page, request: APIRequestContext, status = 200) {
  const pid = status === 200 ? await principals(request) : {};
  await page.route("**/v1/me/waiting", (r) => status === 200
    ? r.fulfill({ status, contentType: "application/json", body: JSON.stringify(fixture(pid)) })
    : r.fulfill({ status, contentType: "application/problem+json", body: JSON.stringify({ code: "not_found", title: "Not Found" }) }));
}

/** The top of the rail (brand + Work group) so the badge is legible. */
async function railShot(page: Page, name: string) {
  await settle(page);
  const box = await page.locator("aside.rail").boundingBox();
  if (!box) throw new Error("no rail");
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), clip: { x: box.x, y: box.y, width: box.width, height: Math.min(box.height, 260) } });
}

for (const theme of ["light", "dark"] as const) {
  test(`waiting on you ${USER} ${theme}${MOCK ? " (mocked API)" : ""}`, async ({ page, request }) => {
    if (MOCK) await mock(page, request);
    await as(page, USER, theme);
    await page.goto("/waiting");
    const rows = page.locator("tr[data-waiting-kind]");
    const empty = page.getByRole("heading", { name: "Nothing is waiting on you" });
    await expect(rows.first().or(empty)).toBeVisible();
    const n = await rows.count();
    const badge = page.locator('nav a[href="/waiting"] .count');
    if (n === 0) await expect(badge).toHaveCount(0);
    else await expect(badge).toHaveText(String(n));
    if (!MOCK) {
      const api = await apiGet<WaitingBody>(request, "/v1/me/waiting", USER);
      expect(api.counts.total, "counts.total matches the rows").toBe(n);
    }
    await expect(page).toHaveTitle(n ? `(${n}) replicant` : "replicant");
    await shot(page, `waiting-1440-${theme}`);
    await page.goto("/jobs");                              // the badge as seen from another screen
    if (n) await expect(badge).toHaveText(String(n));
    await railShot(page, `waiting-badge-1440-${theme}`);
    if (MOCK) {
      await page.setViewportSize({ width: 1024, height: 768 });
      await page.goto("/waiting");
      await rows.first().waitFor();
      await shot(page, `waiting-1024-${theme}`);
    }
  });
}

test("older API without /v1/me/waiting: no badge, plain empty state", async ({ page, request }) => {
  test.skip(!MOCK, "needs WAITING_MOCK=1");
  await mock(page, request, 404);
  await as(page, USER, "light");
  await page.goto("/waiting");
  await expect(page.getByText("does not serve")).toBeVisible();
  await expect(page.locator('nav a[href="/waiting"] .count')).toHaveCount(0);
  await expect(page.locator(".banner.danger")).toHaveCount(0);
  await shot(page, "waiting-unavailable-1440-light");
});
