// Cancel a real running job from the job detail page (CANCEL_JOB_ID from `live_gate.py start`), as the tenant admin.
import { expect, test } from "@playwright/test";
import { apiGet, as, env, shot } from "./helpers";

test("cancelling a real running job from the UI", async ({ page, request }) => {
  test.skip(!process.env.CANCEL_JOB_ID, "set CANCEL_JOB_ID");
  const jobId = env("CANCEL_JOB_ID");
  await as(page, "erin");
  await page.goto(`/jobs/${jobId}`);
  await page.getByTestId("cancel-job").click();
  await expect(page.getByRole("dialog", { name: "Cancel job" })).toBeVisible();
  await page.getByLabel(/Reason/).fill("Cancelled from control-plane-web (Playwright live E2E).");
  await shot(page, "job-cancel-dialog-1440-light");
  await page.getByTestId("confirm-cancel").click();
  await expect(page.locator(".actres")).toContainText(/Cancel: Recorded|Still recording/);
  await expect.poll(async () => (await apiGet<{ snapshot: { status: string } }>(request, `/v1/jobs/${jobId}`)).snapshot.status, { timeout: 120_000 }).toBe("cancelled");
  await page.reload();
  await expect(page.locator(".jobhead .pill").first()).toContainText("Cancelled");
  await shot(page, "job-detail-cancelled-1440-light");
});
