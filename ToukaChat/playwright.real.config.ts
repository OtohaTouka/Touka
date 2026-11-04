import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { OUTPUT_DIR, WEB_ORIGIN } from "./e2e-real/lib/constants";

// 實際驗證：真前端、真後端（沙盒）、假模型（ADR-0295）。跑法：npm run test:e2e:real
export default defineConfig({
  testDir: "./e2e-real",
  globalSetup: "./e2e-real/global-setup.ts",
  outputDir: path.join(OUTPUT_DIR, "results"),
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 180_000,
  expect: { timeout: 30_000 },
  reporter: "line",
  use: {
    baseURL: WEB_ORIGIN,
    headless: true,
    locale: "zh-TW",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
