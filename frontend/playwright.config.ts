import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against a running backend that serves the built terminal
 * at /app, e.g. (from the repo root):
 *
 *   MARKET_DATA_PROVIDER=synthetic COOKIE_SECURE=0 JWT_SECRET=... DATABASE_URL=... \
 *     python -m uvicorn server:app --port 8010
 *
 * and a user created with `python -m backend.app.cli create-user`.
 * E2E_BASE_URL / E2E_EMAIL / E2E_PASSWORD override the defaults below.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  reporter: [["list"]],
  outputDir: "./e2e-results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8010",
    viewport: { width: 1600, height: 900 },
    trace: "retain-on-failure",
    // for staging stacks with a self-signed certificate (never against production)
    ignoreHTTPSErrors: process.env.E2E_IGNORE_HTTPS_ERRORS === "1",
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "chromium",
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"], viewport: { width: 1600, height: 900 }, storageState: "e2e/.auth/user.json" },
    },
  ],
});
