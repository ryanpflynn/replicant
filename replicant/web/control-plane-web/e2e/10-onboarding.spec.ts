// Repo onboarding (design §8c): the Readiness tab, the model tier / repo configuration / readiness override settings
// (dry runs only, through config:validate; nothing is saved), the /admin/fleet impact queue for carol
// (replicant-admins) and its 403 page for erin, and the "Repo configuration" panel on a run that recorded one
// (tests/e2e/e2e08_onboarding.py leaves such jobs; the spec finds the most recent one through the API).
// Screenshots: screenshots/onboarding/ (SHOTS_DIR overrides).
import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { apiGet, as, settle, SHOTS } from "./helpers";

const DIR = path.join(SHOTS, "onboarding");
const PRJ = "prj_ledger";
async function shot(page: Page, name: string) {
  fs.mkdirSync(DIR, { recursive: true });
  await settle(page);
  await page.screenshot({ path: path.join(DIR, `${name}.png`), fullPage: true });
}

test.describe.configure({ mode: "serial" });

test("erin (tenant admin) gets the fleet 403 page and no Fleet nav", async ({ page }) => {
  await as(page, "erin", "light", null);
  await page.goto("/admin/fleet");
  await expect(page.getByText("This page is limited to replicant admins")).toBeVisible();
  await expect(page.getByRole("link", { name: "Fleet" })).toHaveCount(0);
  await shot(page, "fleet-forbidden-erin-1440-light");
  const r = await page.request.get("/v1/admin/fleet/items?kind=mcp_server&sort=impact", { headers: { "X-Replicant-Dev-User": "erin" } });
  expect(r.status()).toBe(403);
});

for (const theme of ["light", "dark"] as const) {
  test(`readiness tab for a project admin (${theme})`, async ({ page }) => {
    await as(page, "erin", theme, null);
    await page.goto(`/projects/${PRJ}/settings/readiness`);
    const card = page.getByRole("region", { name: /Readiness of .*ledger-api/ });
    await expect(card).toBeVisible();
    await expect(card.getByLabel("Readiness status")).toContainText(/Ready|Warnings|Blocked|Not checked yet/);
    await expect(card.getByRole("table", { name: "Commands" })).toContainText("test");
    await expect(card.getByRole("table", { name: "Commands" })).toContainText(/CI:|default for|from /);
    await expect(card.getByLabel("Environment")).toContainText("go-service");
    await expect(card.getByRole("button", { name: "Re-check" })).toBeVisible();
    await shot(page, `readiness-erin-1440-${theme}`);
  });
}

test("a viewer sees readiness without Re-check or Request support", async ({ page }) => {
  await as(page, "dave", "light", null);
  await page.goto(`/projects/${PRJ}/settings/readiness`);
  await expect(page.getByRole("region", { name: /Readiness of / }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Re-check" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Request support" })).toHaveCount(0);
  await shot(page, "readiness-viewer-dave-1440-light");
});

test("a blocked repo shows its cause and a support request dialog", async ({ page }) => {
  await as(page, "grace", "light", null);
  const projects = await apiGet<{ items: Array<{ project_id: string }> }>(page.request, "/v1/projects");
  let prj = "";
  for (const p of projects.items) {
    const d = await apiGet<{ repos: Array<{ status: string; warnings?: unknown[] }> }>(page.request, `/v1/projects/${p.project_id}/readiness`);
    if (d.repos.some((r) => r.status === "blocked" || r.warnings?.length)) { prj = p.project_id; break; }
  }
  test.skip(!prj, "no repo with warnings or blocked yet (run tests/e2e/e2e08_onboarding.py first)");
  await page.goto(`/projects/${prj}/settings/readiness`);
  const blocked = page.getByRole("region", { name: /Readiness of / }).filter({ hasText: "Blocked" });
  if (await blocked.count()) {
    await expect(blocked.first().getByLabel("Last check")).toContainText(/A tool is missing|Tests failed|Build failed|Install failed|Timed out|Needed the network/);
  }
  await shot(page, "readiness-warnings-grace-1440-light");
  const req = page.getByRole("button", { name: "Request support" }).first();
  if (await req.count()) {
    await req.click();
    const dlg = page.getByRole("dialog", { name: "Request support" });
    await expect(dlg.getByRole("button", { name: "Send request" })).toBeDisabled();   // reason required
    await dlg.getByLabel("Support reason").fill("Playwright: our tests need this");
    await expect(dlg.getByRole("button", { name: "Send request" })).toBeEnabled();
    await shot(page, "readiness-request-dialog-grace-1440-light");
    await dlg.getByRole("button", { name: "Cancel" }).click();   // not sent: no audit noise from the spec
  }
});

test("models, repo configuration and readiness override go through review (dry run)", async ({ page }) => {
  await as(page, "erin", "light", null);
  await page.goto(`/projects/${PRJ}/settings/workflow`);
  await expect(page.getByLabel("Default model tier")).toBeEnabled();
  await page.getByLabel("Model tier for bugfix").selectOption("thorough");
  await page.getByLabel("Use the repo's Claude Code configuration").uncheck();
  await shot(page, "workflow-models-erin-1440-light");
  await page.getByRole("button", { name: "Review changes…" }).click();
  const dlg = page.getByRole("dialog", { name: "Review configuration change" });
  await expect(dlg).toContainText("models");
  await expect(dlg).toContainText("repo_config");
  await expect(dlg.getByText("Applies to new jobs").first()).toBeVisible();
  await shot(page, "workflow-models-review-erin-1440-light");
  await dlg.getByRole("button", { name: "Cancel" }).click();

  await page.goto(`/projects/${PRJ}/settings/triggers`);
  const ov = page.getByLabel("Let jobs run on repos that aren't ready");
  await expect(ov).toBeEnabled();
  await ov.check();
  await page.getByRole("button", { name: "Review changes…" }).click();
  const d2 = page.getByRole("dialog", { name: "Review configuration change" });
  await expect(d2).toContainText("triggers.readiness_override");
  await expect(d2.getByText("Applies immediately").first()).toBeVisible();
  await shot(page, "triggers-readiness-override-review-erin-1440-light");
  await d2.getByRole("button", { name: "Cancel" }).click();
});

for (const theme of ["light", "dark"] as const) {
  test(`carol reviews the fleet queue (${theme})`, async ({ page }) => {
    await as(page, "carol", theme, null);
    await page.goto("/admin/fleet?tab=mcp_server");
    await expect(page.getByRole("navigation", { name: "Fleet sections" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Fleet" }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Rescan all" })).toBeVisible();
    await shot(page, `fleet-mcp-carol-1440-${theme}`);
    const rows = page.getByRole("table", { name: "Fleet items" }).getByRole("row");
    if (await rows.count() > 1) {
      await rows.nth(1).click();
      const d = page.getByLabel("Item detail");
      await expect(d).toContainText("Recent jobs");
      await expect(d.getByRole("button", { name: "Approve" })).toBeDisabled();   // reason required
      await d.getByLabel("Decision reason").fill("playwright check");
      await expect(d.getByRole("button", { name: "Approve" })).toBeEnabled();
      await expect(d.getByRole("button", { name: "Map" })).toBeDisabled();       // needs a platform service
      await shot(page, `fleet-item-detail-carol-1440-${theme}`);
    }
    for (const tab of ["hook", "toolchain", "probe_failure"]) {
      await page.goto(`/admin/fleet?tab=${tab}`);
      await expect(page.getByRole("navigation", { name: "Fleet sections" })).toBeVisible();
      await shot(page, `fleet-${tab}-carol-1440-${theme}`);
    }
    await page.goto("/admin/fleet?tab=mcp_server&requests=1");
    await expect(page.getByLabel("Requests")).toBeChecked();
    await shot(page, `fleet-requests-carol-1440-${theme}`);
  });
}

test("Repo configuration panel on a run that recorded one", async ({ page }) => {
  await as(page, "grace", "light", "detailed");
  type G = { graph?: { nodes: Array<{ key: string; runs: Array<{ run_id: string }> }> } | null };
  const jobs = await apiGet<{ items: Array<{ job_id: string }> }>(page.request, "/v1/jobs?limit=30&sort=updated_desc");
  let href = "";
  outer: for (const j of jobs.items) {
    const g = await apiGet<G>(page.request, `/v1/jobs/${j.job_id}/graph`);
    for (const n of g.graph?.nodes ?? []) {
      for (const r of n.runs) {
        const a = await apiGet<{ result?: { cli?: { repo_config?: unknown } } }>(page.request, `/v1/jobs/${j.job_id}/runs/${r.run_id}/activity`);
        if (a.result?.cli?.repo_config) { href = `/jobs/${j.job_id}/graph?node=${encodeURIComponent(n.key)}&run=${encodeURIComponent(r.run_id)}`; break outer; }
      }
    }
  }
  test.skip(!href, "no run with a repo_config section yet (run tests/e2e/e2e08_onboarding.py first)");
  await page.goto(href);
  const panel = page.getByLabel("Repo configuration", { exact: true });
  await expect(panel).toContainText(/Used the repo's Claude Code setup|Repo configuration not used/);
  await shot(page, "run-repo-config-grace-1440-light");
});
