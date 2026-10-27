// Memory (MEM): the admin curation screens for carol (replicant-admins), the 403 page for erin (tenant admin), and the
// read-only "Memory used" panels on a job that received memory (tests/e2e/e2e06_memory.py leaves such jobs; the spec
// finds the most recent one through the API). The spec authors one entry in tenant local-dev (no payments job ever
// selects it) and purges it at the end. Screenshots: screenshots/memory/ (SHOTS_DIR overrides).
import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { as, settle, SHOTS } from "./helpers";

const DIR = path.join(SHOTS, "memory");
async function shot(page: Page, name: string) {
  fs.mkdirSync(DIR, { recursive: true });
  await settle(page);
  await page.screenshot({ path: path.join(DIR, `${name}.png`), fullPage: true });
}
const H = (u: string) => ({ "X-Replicant-Dev-User": u, "Idempotency-Key": `pw-${Date.now()}-${Math.random().toString(36).slice(2)}` });

test.describe.configure({ mode: "serial" });
let created = "";

test("erin (tenant admin) gets the 403 page", async ({ page }) => {
  await as(page, "erin", "light", null);
  await page.goto("/admin/memory");
  await expect(page.getByText("This page is limited to replicant admins")).toBeVisible();
  await expect(page.getByRole("link", { name: "Memory" })).toHaveCount(0);
  await shot(page, "forbidden-erin-1440-light");
  const r = await page.request.get("/v1/admin/memory/candidates", { headers: { "X-Replicant-Dev-User": "erin" } });
  expect(r.status()).toBe(403);
  expect((await r.json()).audit_id).toMatch(/^aud_/);
});

for (const theme of ["light", "dark"] as const) {
  test(`carol curates memory (${theme})`, async ({ page }) => {
    await as(page, "carol", theme, null);
    if (!created) {
      const r = await page.request.post("/v1/admin/memory/entries", { headers: H("carol"), data: {
        tenant_id: "local-dev", memory_type: "rule", scope_level: "tenant", title: "Playwright entry",
        statement: "Playwright check: prefer small, reviewable changes with a test for every fix." } });
      expect(r.status(), await r.text()).toBe(201);
      created = (await r.json()).entry_id;
    }
    await page.goto("/admin/memory?tab=candidates");
    await expect(page.getByRole("navigation", { name: "Memory sections" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Memory" }).first()).toBeVisible();
    await shot(page, `candidates-carol-1440-${theme}`);
    await page.goto("/admin/memory?tab=entries");
    await expect(page.getByRole("table", { name: "Entries" })).toContainText("Playwright entry");
    await page.getByRole("table", { name: "Entries" }).getByText("Playwright entry").click();
    const detail = page.getByLabel("Entry detail");
    await expect(detail).toContainText("Provenance");
    await expect(detail.getByRole("button", { name: "Disable" })).toBeVisible();
    await shot(page, `entry-detail-carol-1440-${theme}`);
    await page.goto("/admin/memory?tab=quarantine");
    await page.getByLabel("Entry IDs").fill(created);
    await page.getByLabel("Purge").getByRole("button", { name: "Dry run" }).click();
    await expect(page.getByLabel("Blast radius")).toContainText("dry run");
    await shot(page, `quarantine-purge-dryrun-carol-1440-${theme}`);
    await page.goto("/admin/memory?tab=conflicts");
    await shot(page, `conflicts-carol-1440-${theme}`);
    await page.goto("/admin/memory?tab=analytics");
    await expect(page.getByLabel("Summary")).toContainText("Entries");
    await shot(page, `analytics-carol-1440-${theme}`);
    await page.goto("/admin/memory?tab=export");
    await shot(page, `export-carol-1440-${theme}`);
  });
}

test("Memory used panels on a job that received memory", async ({ page }) => {
  await as(page, "grace", "light", "detailed");
  const jobs = await (await page.request.get("/v1/jobs?limit=50", { headers: { "X-Replicant-Dev-User": "grace" } })).json();
  let jobId = "";
  for (const j of jobs.items) {
    const m = await (await page.request.get(`/v1/jobs/${j.job_id}/memory`, { headers: { "X-Replicant-Dev-User": "grace" } })).json();
    if (m.available && m.entries.some((e: { purged?: boolean }) => !e.purged)) { jobId = j.job_id; break; }
  }
  test.skip(!jobId, "no job with memory yet (run tests/e2e/e2e06_memory.py first)");
  await page.goto(`/jobs/${jobId}`);
  const panel = page.getByLabel("Memory used");
  await expect(panel).toContainText("runs received memory");
  await shot(page, "job-detail-memory-used-grace-1440-light");
  await as(page, "dave", "light", "simple");
  await page.goto(`/jobs/${jobId}`);
  await expect(page.getByTestId("job-simple")).toBeVisible();
  await expect(page.getByLabel("Memory used")).toContainText("from memory");
  await shot(page, "job-simple-memory-line-dave-1440-light");
});

test.afterAll(async ({ request }) => {
  if (created) {
    await request.post("/v1/admin/memory/purges", { headers: H("carol"),
      data: { entry_ids: [created], reason_code: "curator", reason_text: "playwright cleanup" } });
  }
});
