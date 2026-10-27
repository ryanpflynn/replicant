// Simple job view and the per-user job-view preference (WEB-UIPREF). Live against the stack: uses SIMPLE_JOB_ID or the
// most recent job. Checks that Simple is the default, that "Show details" / "Simple view" persists across reloads,
// and that the choice is per user (switching the dev user shows that user's view). Screenshots: screenshots/simple/.
import * as path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { apiGet, as, SHOTS, settle } from "./helpers";

const DIR = path.join(SHOTS, "simple");

async function jobId(request: APIRequestContext): Promise<string> {
  if (process.env.SIMPLE_JOB_ID) return process.env.SIMPLE_JOB_ID;
  const r = await apiGet<{ items: Array<{ job_id: string }> }>(request, "/v1/jobs?limit=1&sort=updated_desc");
  expect(r.items.length, "the stack has at least one job").toBeGreaterThan(0);
  return r.items[0].job_id;
}

/** Sets a user's job view through the API (no version: last writer wins), so each test starts from a known state. */
async function setView(request: APIRequestContext, user: string, view: "simple" | "detailed") {
  const r = await request.put("/v1/me/ui-preferences", { headers: { "X-Replicant-Dev-User": user }, data: { job_view: view } });
  expect(r.ok(), `PUT ui-preferences ${user} -> ${r.status()}`).toBe(true);
}

const simple = (page: Page) => page.getByTestId("job-simple");
const detailed = (page: Page) => page.locator('nav[aria-label="Job sections"]');

async function snap(page: Page, name: string) {
  await settle(page);
  await page.screenshot({ path: path.join(DIR, `${name}.png`), fullPage: true });
}

test("a fresh user gets the Simple view", async ({ page, request }) => {
  const job = await jobId(request);
  // Server default: a user with no stored preference (version 0) is on Simple, in both endpoints.
  const pref = await apiGet<{ job_view: string; version: number }>(request, "/v1/me/ui-preferences", "dave");
  if (pref.version === 0) {
    expect(pref.job_view).toBe("simple");
    expect((await apiGet<{ ui_preferences: { job_view: string } }>(request, "/v1/me", "dave")).ui_preferences.job_view).toBe("simple");
  }
  // Client default: /v1/me with no ui_preferences at all (a fresh user, or an API without WEB-UIPREF) shows Simple.
  await page.route("**/v1/me", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    delete body.ui_preferences;
    await route.fulfill({ response: res, json: body });
  });
  await as(page, "dave", "light", null);
  await page.goto(`/jobs/${job}`);
  await expect(simple(page)).toBeVisible();
  await expect(page.getByTestId("headline")).not.toHaveText("…");
  await expect(page.locator('ol[aria-label="Progress"] > li')).toHaveCount(6);
  await expect(page.getByTestId("needed")).toContainText("What's needed from you");
  await expect(detailed(page)).toHaveCount(0);
});

test("the toggle persists across reload", async ({ page, request }) => {
  const job = await jobId(request);
  await setView(request, "erin", "simple");
  await as(page, "erin", "light", null);
  await page.goto(`/jobs/${job}`);
  await expect(simple(page)).toBeVisible();
  await page.getByTestId("view-toggle").click();                          // Show details
  await expect(detailed(page)).toBeVisible();
  await expect.poll(async () => (await apiGet<{ job_view: string }>(request, "/v1/me/ui-preferences", "erin")).job_view).toBe("detailed");
  await page.reload();
  await expect(detailed(page)).toBeVisible();
  await expect(simple(page)).toHaveCount(0);
  await page.getByTestId("view-toggle").click();                          // Simple view
  await expect(simple(page)).toBeVisible();
  await expect.poll(async () => (await apiGet<{ job_view: string }>(request, "/v1/me/ui-preferences", "erin")).job_view).toBe("simple");
  await page.reload();
  await expect(simple(page)).toBeVisible();
  // a sub-page link always opens the Detailed page it names; "Simple view" there goes back to the job
  await page.goto(`/jobs/${job}/runs`);
  await expect(detailed(page)).toBeVisible();
  await page.getByTestId("view-toggle").click();
  await expect(page).toHaveURL(new RegExp(`/jobs/${job}$`));
  await expect(simple(page)).toBeVisible();
});

test("the view is per user", async ({ page, request }) => {
  const job = await jobId(request);
  await setView(request, "erin", "simple");
  await setView(request, "alice", "simple");
  await as(page, "erin", "light", null);
  await page.goto(`/jobs/${job}`);
  await page.getByTestId("view-toggle").click();
  await expect(detailed(page)).toBeVisible();
  await page.locator(".rail .me").click();                                  // switch dev user to alice
  await page.getByRole("menuitemradio").filter({ hasText: /alice/i }).click();
  await expect(simple(page)).toBeVisible();
  await page.locator(".rail .me").click();                                  // and back: erin is still on Detailed
  await page.getByRole("menuitemradio").filter({ hasText: /erin/i }).click();
  await expect(detailed(page)).toBeVisible();
  await setView(request, "erin", "simple");
});

for (const theme of ["light", "dark"] as const) {
  for (const [w, h] of [[1440, 900], [390, 844]] as const) {
    test(`screenshots ${w} ${theme}`, async ({ page, request }) => {
      const job = await jobId(request);
      await setView(request, "grace", "simple");
      await page.setViewportSize({ width: w, height: h });
      await as(page, "grace", theme, null);
      await page.goto(`/jobs/${job}`);
      await expect(simple(page)).toBeVisible();
      await expect(page.locator(".sms li").first()).toBeVisible();
      const wide = await page.evaluate(() => document.querySelector(".simple")!.scrollWidth > document.querySelector(".simple")!.clientWidth + 1);
      expect(wide, "no horizontal overflow in the Simple view").toBe(false);
      await snap(page, `job-simple-${w}-${theme}`);

      // The same job replayed as "waiting at the publish gate" (route interception), to show the decision card.
      const d = await apiGet<Record<string, any>>(request, `/v1/jobs/${job}`, "grace");
      const gate = (d.snapshot.gates ?? [])[0];
      test.skip(!gate, "the job has no gate to replay");
      const tl = await apiGet<{ items: Array<{ type: string }> }>(request, `/v1/jobs/${job}/timeline?limit=200`, "grace");
      const cut = tl.items.findIndex((e) => e.type === "gate.decided");
      const g = await apiGet<Record<string, any>>(request, `/v1/gates/${gate.gate_id}`, "grace");
      const open = { ...gate, state: "open", decision: undefined, decision_log: undefined };
      await page.route(`**/v1/jobs/${job}`, (r) => r.fulfill({ json: { ...d, snapshot: { ...d.snapshot, status: "waiting_human", phase: "publish_gate",
        terminal: undefined, pull_request: undefined, gates: [open] } } }));
      await page.route(`**/v1/jobs/${job}/timeline*`, (r) => r.fulfill({ json: { items: cut > 0 ? tl.items.slice(0, cut) : tl.items } }));
      await page.route(`**/v1/gates/${gate.gate_id}`, (r) => r.fulfill({ json: { ...g, gate: open, decidable: true } }));
      await page.reload();
      await expect(page.getByTestId("approve")).toBeVisible();
      await expect(page.getByTestId("headline")).toContainText("Waiting for an approver");
      await snap(page, `job-simple-waiting-${w}-${theme}`);
    });
  }
}
