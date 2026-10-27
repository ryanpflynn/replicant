// Live assertions against real data, ending with a real gate approval from the UI.
import { expect, test } from "@playwright/test";
import { apiGet, as, env, shot } from "./helpers";

interface JobSummary { job_id: string; title: string; status: string }
interface JobDetail { snapshot: { title: string; status: string; runs: Array<{ run_id: string }>; gates: Array<{ gate_id: string; state: string }> } }

test.describe.configure({ mode: "serial" });

test("jobs list shows the real jobs from control-plane-api", async ({ page, request }) => {
  const open = await apiGet<{ items: JobSummary[] }>(request, "/v1/jobs?status=queued,running,waiting_human,waiting_external,paused&limit=200");
  const all = await apiGet<{ items: JobSummary[] }>(request, "/v1/jobs?limit=200");
  expect(all.items.length).toBeGreaterThan(0);
  await as(page, "grace");
  await page.goto("/jobs");
  for (const j of open.items) await expect(page.locator(`tr[data-job-id="${j.job_id}"]`)).toBeVisible();
  await expect(page.locator("tbody tr[data-job-id]")).toHaveCount(open.items.length);
  await page.goto("/jobs?view=recent");
  const recent = all.items.filter((j) => !open.items.some((o) => o.job_id === j.job_id));
  await expect(page.locator("tbody tr[data-job-id]")).toHaveCount(recent.length);
  const first = recent[0];
  await expect(page.locator(`tr[data-job-id="${first.job_id}"] td.title`)).toContainText(first.title);
});

test("job detail renders a real job", async ({ page, request }) => {
  const jobId = env("JOB_ID");
  const d = await apiGet<JobDetail>(request, `/v1/jobs/${jobId}`);
  await as(page, "grace");
  await page.goto(`/jobs/${jobId}`);
  await expect(page.locator(".jobhead h1")).toHaveText(d.snapshot.title);
  await expect(page.locator(".jobhead .pill").first()).toContainText("Waiting on human");
  await expect(page.locator("ol.tl li").first()).toBeVisible();
  await expect(page.getByRole("link", { name: /Grafana job drilldown/ })).toHaveAttribute("href", new RegExp(`var-job_id=${jobId}`));
  await expect(page.getByRole("link", { name: /Temporal workflow/ })).toHaveAttribute("href", new RegExp(`workflows/${jobId}`));
  await page.getByRole("link", { name: /^Runs/ }).click();
  for (const r of d.snapshot.runs) await expect(page.getByLabel(r.run_id)).toBeVisible();
});

test("approving the real pending gate from the UI works end to end", async ({ page, request }) => {
  const jobId = env("JOB_ID");
  const gateId = env("GATE_ID");
  const before = await apiGet<{ gate: { state: string } }>(request, `/v1/gates/${gateId}`);
  expect(before.gate.state).toBe("open");
  await as(page, "grace");
  await page.goto("/approvals");
  await page.locator(`a.gitem[data-gate-id="${gateId}"]`).click();
  await expect(page.locator(".packet .pill.attention")).toContainText("Waiting on you");
  await expect(page.locator(".diff .fh .path").first()).toHaveText("internal/ledger/ledger.go");
  await page.getByLabel("Decision comment").fill("Approved from control-plane-web (Playwright live E2E).");
  await page.getByTestId("approve").click();
  await expect(page.getByTestId("decision-outcome")).toContainText(/Approved by you in the UI|Still recording/);
  await shot(page, "approvals-approved-1440-light");
  await expect.poll(async () => (await apiGet<{ gate: { state: string } }>(request, `/v1/gates/${gateId}`)).gate.state, { timeout: 60_000 }).toBe("approved");
  await expect.poll(async () => (await apiGet<JobDetail>(request, `/v1/jobs/${jobId}`)).snapshot.status, { timeout: 120_000 })
    .not.toBe("waiting_human");
});
