import { defineConfig, devices } from "@playwright/test";

/**
 * The panel against the local stack: `make up`, `make seed`, then `make web-e2e`.
 * The API answers at API_ORIGIN (default http://localhost:8000); the panel is built with that
 * origin and served on WEB_PORT. Screenshots land in test-results/ for the design comparison.
 */
const port = Number(process.env.WEB_PORT ?? 3000);
const apiOrigin = process.env.API_ORIGIN ?? "http://localhost:8000";
const baseURL = `http://localhost:${port}`;

export default defineConfig({
  testDir: "e2e",
  outputDir: "test-results/artifacts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL, trace: "retain-on-failure", locale: "en-US" },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "signed-out",
      testMatch: /login\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "print",
      testMatch: /report\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "panel",
      testMatch: /(overview|threats|threat-detail|whitelist|account)\.spec\.ts/,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"], storageState: "e2e/.auth/owner.json" },
    },
    {
      name: "viewer",
      testMatch: /roles\.spec\.ts/,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"], storageState: "e2e/.auth/viewer.json" },
    },
  ],
  webServer: {
    command: `npm run build && npx next start --port ${port}`,
    url: `${baseURL}/login`,
    reuseExistingServer: true,
    timeout: 240_000,
    env: { API_ORIGIN: apiOrigin, NEXT_TELEMETRY_DISABLED: "1" },
  },
});
