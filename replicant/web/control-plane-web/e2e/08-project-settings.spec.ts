// Project settings (CONFIG-01): the same screens read-only for a viewer and editable for a tenant admin, the
// "applies immediately / to new jobs" badges, the review dialog, the workflow preview, access and history.
// Dry runs only (config:validate): nothing is saved, so it can run next to tests/e2e/e2e05_config.py.
// Screenshots: screenshots/settings/ (SHOTS_DIR overrides).
import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { as, settle, SHOTS } from "./helpers";

const DIR = path.join(SHOTS, "settings");
const PRJ = "prj_ledger";
async function shot(page: Page, name: string) {
  fs.mkdirSync(DIR, { recursive: true });
  await settle(page);
  await page.screenshot({ path: path.join(DIR, `${name}.png`), fullPage: true });
}

test.describe.configure({ mode: "serial" });

for (const theme of ["light", "dark"] as const) {
  test(`viewer sees read-only settings (${theme})`, async ({ page }) => {
    await as(page, "dave", theme, null);
    await page.goto("/projects");
    await expect(page.getByRole("link", { name: "Projects" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Projects" })).toContainText("read-only");
    await page.goto(`/projects/${PRJ}/settings/triggers`);
    await expect(page.getByText("Read-only.")).toBeVisible();
    await expect(page.getByLabel("Trigger label")).toBeDisabled();
    await expect(page.getByText("Applies immediately").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Review changes…" })).toHaveCount(0);
    await shot(page, `triggers-readonly-dave-1440-${theme}`);
    await page.goto(`/projects/${PRJ}/settings/approvals`);
    await expect(page.getByLabel("Plan approval")).toBeDisabled();
    await expect(page.getByText("Applies to new jobs").first()).toBeVisible();
    await shot(page, `approvals-readonly-dave-1440-${theme}`);
  });

  test(`tenant admin edits, reviews and previews (${theme})`, async ({ page }) => {
    await as(page, "erin", theme, null);
    await page.goto(`/projects/${PRJ}/settings/overview`);
    await expect(page.getByText("acme-payments/ledger-api")).toBeVisible();
    await shot(page, `overview-erin-1440-${theme}`);

    await page.goto(`/projects/${PRJ}/settings/triggers`);
    const label = page.getByLabel("Trigger label");
    await expect(label).toBeEnabled();
    await label.fill("replicant-preview");
    await page.goto(`/projects/${PRJ}/settings/approvals`);   // the draft is kept per screen visit only
    await expect(page.getByLabel("Plan approval")).toBeEnabled();
    await page.getByLabel("Plan approval").selectOption("risk_medium");   // e2e05 never uses risk_medium
    await page.getByLabel("Large change threshold").fill("7");
    await page.getByRole("button", { name: "Review changes…" }).click();
    const dlg = page.getByRole("dialog", { name: "Review configuration change" });
    await expect(dlg).toContainText("gates.plan");
    await expect(dlg).toContainText("risk.large_change_files");
    await expect(dlg.getByText("Applies to new jobs").first()).toBeVisible();
    await expect(dlg.getByRole("button", { name: "Save version" })).toBeDisabled();   // reason required
    await shot(page, `approvals-review-erin-1440-${theme}`);
    await dlg.getByRole("button", { name: "Cancel" }).click();

    await page.goto(`/projects/${PRJ}/settings/workflow`);
    await page.getByLabel("Max review rounds").fill("1");
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await expect(page.locator(".cfgpreview svg")).toBeVisible();
    await expect(page.locator(".cfgpreview")).toContainText("graph/bugfix-c");
    await shot(page, `workflow-preview-erin-1440-${theme}`);
    // Advanced YAML with an unknown node type is rejected by the server-side validation
    await page.getByLabel("Edit the graph as YAML").check();
    const yaml = page.getByLabel("Graph YAML");
    await yaml.fill((await yaml.inputValue()).replace("type: context_assembler", "type: teleport_run"));
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await expect(page.getByText("unknown node type 'teleport_run'")).toBeVisible();
    await shot(page, `workflow-yaml-invalid-erin-1440-${theme}`);

    await page.goto(`/projects/${PRJ}/settings/access`);
    await expect(page.getByRole("table", { name: "Project role bindings" })).toContainText("project_admin");
    await expect(page.getByRole("button", { name: "Save access" })).toBeVisible();
    await shot(page, `access-erin-1440-${theme}`);
    await page.goto(`/projects/${PRJ}/settings/history`);
    await expect(page.getByRole("table", { name: "Configuration versions" })).toBeVisible();
    await shot(page, `history-erin-1440-${theme}`);
  });
}

test("project admin cannot open another project", async ({ page }) => {
  await as(page, "frank", "light", null);
  await page.goto("/projects");
  await expect(page.getByRole("table", { name: "Projects" })).toContainText("Ledger");
  await expect(page.getByRole("table", { name: "Projects" })).not.toContainText("Payments web");
  await page.goto("/projects/prj_payments-web/settings/overview");
  await expect(page.getByText("403 · this project")).toBeVisible();
  await shot(page, "other-project-forbidden-frank-1440-light");
});
