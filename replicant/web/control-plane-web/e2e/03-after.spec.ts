// After the approval: the job moves on (PR opened) and the gate shows as decided, in both themes.
// JOB_ID (from live_gate.py make, approved by 02-live) or, without it, the most recent succeeded job on the stack.
// Whoever approved it (Grace in the 01-03 chain, anyone in a demo or e2e run), the timeline names an approver.
import { expect, test, type APIRequestContext } from "@playwright/test";
import { apiGet, as, shot } from "./helpers";

async function jobId(request: APIRequestContext): Promise<string> {
  if (process.env.JOB_ID) return process.env.JOB_ID;
  const r = await apiGet<{ items: Array<{ job_id: string }> }>(request, "/v1/jobs?status=succeeded&limit=1");
  expect(r.items.length, "the stack has a succeeded job (run tests/e2e/demo.py or set JOB_ID)").toBeGreaterThan(0);
  return r.items[0].job_id;
}

for (const theme of ["light", "dark"] as const) {
  test(`after approval ${theme}`, async ({ page, request }) => {
    const id = await jobId(request);
    await as(page, "grace", theme);
    await page.goto(`/jobs/${id}`);
    await expect(page.locator("ol.tl")).toContainText(/approved by \S+/);
    await shot(page, `job-detail-after-approval-1440-${theme}`);
    await page.goto(`/jobs/${id}/gates`);
    await page.locator('table[aria-label="Gates"] tbody tr').first().waitFor();
    await shot(page, `job-detail-gates-1440-${theme}`);
  });
}
