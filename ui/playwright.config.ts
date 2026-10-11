import { defineConfig } from "@playwright/test";

/**
 * Browser checks that jsdom cannot do: axe (including colour contrast), keyboard flows and phone
 * width. The API is mocked in the browser (`e2e/fixtures.ts`), so no controller is needed.
 * Run with `pnpm e2e`; set `PW_CHANNEL=chrome` to use an installed Chrome instead of the bundled
 * Chromium.
 */
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  fullyParallel: true,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5199",
    channel: process.env.PW_CHANNEL || undefined,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 900 } }, testIgnore: /responsive/ },
    { name: "phone", use: { viewport: { width: 375, height: 812 } }, testMatch: /responsive/ },
  ],
  webServer: {
    command: "pnpm exec vite --port 5199 --strictPort",
    url: "http://localhost:5199",
    reuseExistingServer: true,
    env: { SY_API: "http://127.0.0.1:9" },
  },
});
