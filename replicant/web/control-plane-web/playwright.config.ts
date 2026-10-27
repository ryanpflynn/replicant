import { defineConfig, devices } from "@playwright/test";

// Live E2E against the running stack (default http://127.0.0.1:8400). Files run in name order on one worker:
// 01 screenshots of the pending state, 02 live assertions ending in a real gate approval, 03 after-approval shots.
export default defineConfig({
  testDir: "./e2e",
  testMatch: /\d\d-.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    baseURL: process.env.PW_BASE_URL ?? "http://127.0.0.1:8400",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    trace: "retain-on-failure",
  },
});
