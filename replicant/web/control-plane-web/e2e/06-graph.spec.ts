// Graph tab + run activity (GRAPH-*), against a real scripted-backend job on the live stack.
//   GRAPH_JOB_ID=job_… (started moments ago, e.g. `tests/e2e/demo.py start`): mid-run shots while an agent node runs,
//     then the waiting publish gate, then a finished agent run's activity (diff expanded), light + dark.
// Screenshots: screenshots/graph/ (SHOTS_DIR overrides).
import { expect, test, type Page } from "@playwright/test";
import * as path from "node:path";
import { as, env, settle, SHOTS } from "./helpers";

const DIR = process.env.GRAPH_SHOTS_DIR ?? path.join(SHOTS, "graph");
const shot = (page: Page, name: string) => page.screenshot({ path: path.join(DIR, `${name}.png`), fullPage: false });

test.describe.configure({ mode: "serial" });

test("graph mid-run: running agent node and its live activity", async ({ page }) => {
  test.setTimeout(600_000);
  const job = env("GRAPH_JOB_ID");
  await as(page, "alice");
  await page.goto(`/jobs/${job}/graph`);
  await expect(page.locator("svg.gsvg")).toBeVisible({ timeout: 60_000 });
  // an agent node in Running (implement / qa / review); the tab polls every 4 s
  const running = page.locator("g.gn.agent.s-running").first();
  await expect(running).toBeVisible({ timeout: 300_000 });
  const key = await running.getAttribute("data-node");
  await running.click();
  await expect(page.getByTestId("run-activity")).toBeVisible();
  await settle(page);
  await shot(page, "graph-midrun-running-1440-light");
  // the feed fills in as the uploader ships transcript chunks
  await expect(page.locator(".feed .fi.tool").first()).toBeVisible({ timeout: 120_000 }).catch(() => undefined);
  await settle(page);
  await page.setViewportSize({ width: 1440, height: 1500 });
  await shot(page, `activity-live-${key}-1440-light`);
});

test("graph waiting at the publish gate", async ({ page }) => {
  test.setTimeout(600_000);
  const job = env("GRAPH_JOB_ID");
  await as(page, "alice");
  await page.goto(`/jobs/${job}/graph?node=publish_gate`);
  await expect(page.locator('g.gn[data-node="publish_gate"].s-waiting')).toBeVisible({ timeout: 480_000 });
  await settle(page);
  await shot(page, "graph-waiting-gate-1440-light");
  await as(page, "alice", "dark");
  await page.reload();
  await expect(page.locator('g.gn[data-node="publish_gate"].s-waiting')).toBeVisible();
  await settle(page);
  await shot(page, "graph-waiting-gate-1440-dark");
});

test("activity of the finished implementer run", async ({ page }) => {
  const job = env("GRAPH_JOB_ID");
  await page.setViewportSize({ width: 1440, height: 2100 });  // .main scrolls, so a tall viewport shows the feed
  for (const theme of ["light", "dark"] as const) {
    await as(page, "alice", theme);
    await page.goto(`/jobs/${job}/graph?node=implement`);
    const act = page.getByTestId("run-activity");
    await expect(act).toBeVisible();
    await expect(act.locator(".feed .fi.tool").first()).toBeVisible({ timeout: 60_000 });
    // Edit rows open with their diff; also open the Bash row to show its command output
    const bash = act.locator(".feed .fi.tool", { hasText: "Bash" }).first();
    await bash.locator(".frow").click();
    await expect(act.locator(".udiff").first()).toBeVisible();
    await expect(act.locator(".gstats")).toContainText("Tokens in / out");
    await settle(page);
    await act.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(DIR, `activity-implementer-1440-${theme}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1024, height: 1800 });
  await page.goto(`/jobs/${job}/graph?node=qa`);
  await expect(page.getByTestId("run-activity")).toBeVisible();
  await settle(page);
  await page.screenshot({ path: path.join(DIR, "activity-qa-1024-dark.png"), fullPage: true });
});

test("graph of the finished job", async ({ page }) => {
  test.setTimeout(900_000);
  const job = env("GRAPH_JOB_ID");
  await page.setViewportSize({ width: 1440, height: 1500 });
  for (const theme of ["light", "dark"] as const) {
    await as(page, "alice", theme);
    await page.goto(`/jobs/${job}/graph?node=review`);
    await expect(page.locator("g.gn.end")).toBeVisible({ timeout: 600_000 });   // the reached terminal outcome
    await expect(page.locator('g.gn[data-node="publish"].s-succeeded')).toBeVisible();
    await settle(page);
    await shot(page, `graph-complete-1440-${theme}`);
  }
});
