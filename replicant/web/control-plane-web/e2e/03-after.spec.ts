// After the approval: the job moves on (PR opened) and the gate shows as decided, in both themes.
import { expect, test } from "@playwright/test";
import { as, env, shot } from "./helpers";

for (const theme of ["light", "dark"] as const) {
  test(`after approval ${theme}`, async ({ page }) => {
    const jobId = env("JOB_ID");
    await as(page, "grace", theme);
    await page.goto(`/jobs/${jobId}`);
    await expect(page.locator("ol.tl")).toContainText(/approved by Grace/);
    await shot(page, `job-detail-after-approval-1440-${theme}`);
    await page.goto(`/jobs/${jobId}/gates`);
    await page.locator('table[aria-label="Gates"] tbody tr').first().waitFor();
    await shot(page, `job-detail-gates-1440-${theme}`);
  });
}
