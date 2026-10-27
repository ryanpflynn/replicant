// Screenshots of every screen in both themes while a real gate is pending (JOB_ID / GATE_ID from live_gate.py make).
import { test } from "@playwright/test";
import { as, env, shot } from "./helpers";

const JOB = () => env("JOB_ID");
const GATE = () => env("GATE_ID");

for (const theme of ["light", "dark"] as const) {
  test.describe(`screens ${theme}`, () => {
    test(`grace screens ${theme}`, async ({ page }) => {
      await as(page, "grace", theme);
      await page.goto("/jobs");
      await page.locator(`tr[data-job-id="${JOB()}"]`).waitFor();
      await shot(page, `jobs-open-1440-${theme}`);
      await page.goto("/jobs?view=recent");
      await page.locator("tbody tr[data-job-id]").first().waitFor();
      await shot(page, `jobs-recent-1440-${theme}`);
      await page.goto(`/jobs/${JOB()}`);
      await page.locator("ol.tl li").nth(4).waitFor();
      await shot(page, `job-detail-waiting-1440-${theme}`);
      await page.goto(`/jobs/${JOB()}/runs`);
      await page.locator('table[aria-label="Runs"] tbody tr').nth(3).waitFor();
      await shot(page, `job-detail-runs-1440-${theme}`);
      await page.goto(`/jobs/${JOB()}/diff`);
      await page.locator(".diff .fh").first().waitFor();
      await shot(page, `job-detail-diff-1440-${theme}`);
      await page.goto(`/jobs/${JOB()}/qa`);
      await page.locator('table[aria-label="QA stages"]').waitFor();
      await shot(page, `job-detail-qa-1440-${theme}`);
      await page.goto(`/approvals/${GATE()}`);
      await page.locator(".diff .fh").first().waitFor();
      await page.locator('[data-testid="approve"]').waitFor();
      await shot(page, `approvals-pending-1440-${theme}`);
      await page.goto("/approvals?view=decided");
      await page.locator('[data-testid="decision-outcome"]').waitFor();
      await shot(page, `approvals-decided-1440-${theme}`);
      await page.goto("/releases");
      await page.locator('table[aria-label="Release bundles"] tbody tr').first().waitFor();
      await page.locator(".chg li").first().waitFor();
      await shot(page, `releases-1440-${theme}`);
      await page.goto("/admin");
      await page.locator('table[aria-label="Tenants"] tbody tr').first().waitFor();
      await page.locator('table[aria-label="Migration runs"]').waitFor();
      await shot(page, `admin-1440-${theme}`);
      await page.goto("/jobs");
      await page.locator(`tr[data-job-id="${JOB()}"]`).waitFor();
      await page.keyboard.press("Meta+k");
      await page.getByRole("dialog", { name: "Command palette" }).waitFor();
      await page.keyboard.type("refund");
      await shot(page, `palette-1440-${theme}`);
      await page.keyboard.press("Escape");
      await page.getByTestId("user-switcher").click();
      await page.getByRole("menu").waitFor();
      await shot(page, `user-switcher-1440-${theme}`);
    });

    test(`memory views ${theme}`, async ({ page, browser }) => {
      await as(page, "carol", theme);
      await page.goto("/admin/memory");
      await page.getByText("Memory service is not running in v0").waitFor();
      await shot(page, `memory-admin-1440-${theme}`);
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const p2 = await ctx.newPage();
      await as(p2, "alice", theme);
      await p2.goto("/admin/memory");
      await p2.getByText("403 · memory administration").waitFor();
      await shot(p2, `memory-denied-1440-${theme}`);
      await p2.goto("/approvals");
      await p2.getByText("Nothing is waiting on you").waitFor();
      await shot(p2, `approvals-empty-alice-1440-${theme}`);
      await ctx.close();
    });

    test(`1024 ${theme}`, async ({ browser }) => {
      const ctx = await browser.newContext({ viewport: { width: 1024, height: 768 } });
      const page = await ctx.newPage();
      await as(page, "grace", theme);
      await page.goto("/jobs");
      await page.locator(`tr[data-job-id="${JOB()}"]`).waitFor();
      await shot(page, `jobs-open-1024-${theme}`);
      await page.goto(`/jobs/${JOB()}`);
      await page.locator("ol.tl li").nth(4).waitFor();
      await shot(page, `job-detail-waiting-1024-${theme}`);
      await page.goto(`/approvals/${GATE()}`);
      await page.locator(".diff .fh").first().waitFor();
      await shot(page, `approvals-pending-1024-${theme}`);
      await ctx.close();
    });
  });
}
