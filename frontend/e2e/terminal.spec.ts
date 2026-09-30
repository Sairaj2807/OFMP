import { expect, type Page, test } from "@playwright/test";

import { EMAIL, PASSWORD } from "./env";

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const signedOut = { storageState: { cookies: [], origins: [] } };

// Tests use the session saved by auth.setup.ts unless they opt out.
async function openTerminal(page: Page) {
  await page.goto("/app/");
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
}

test.describe("signed out", () => {
  test.use(signedOut);

test("unauthenticated visitors are sent to sign in", async ({ page }) => {
  await page.goto("/app/");
  await page.waitForURL(/\/app\/login\/$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("wrong password shows an error", async ({ page }) => {
  await page.goto("/app/login/");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill("definitely wrong 1");
  await page.getByRole("button", { name: "Sign in" }).click();
  // scoped to the form: Next.js adds its own role=alert route announcer
  await expect(page.locator("form").getByRole("alert")).toContainText("Error:");
});

test("sign out ends the session", async ({ page }) => {
  // its own session, so revoking it does not affect the shared one
  await page.goto("/app/login/");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/app\/$/);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL(/\/app\/login\/$/);
  await page.goto("/app/");
  await page.waitForURL(/\/app\/login\/$/);
});
});

test("terminal streams live data and draws the footprint", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  await openTerminal(page);

  await expect(page.getByRole("status").filter({ hasText: "Live" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByLabel("Active contract")).not.toHaveText("—");
  await expect(page.getByTestId("chart-c1").locator("canvas").first()).toBeVisible();
  // wait for the first trade to be drawn
  await expect(page.getByText("Waiting for the first trade…")).toBeHidden({ timeout: 15_000 });
  await expect(page.getByLabel("Order book")).toContainText("Spread");
  await expect(page.getByLabel("Session figures")).not.toContainText("CVD since start · qty—");
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/terminal-1.png` });
  expect(consoleErrors).toEqual([]);
});

test("layouts, interval changes and the command palette", async ({ page }) => {
  await openTerminal(page);
  await expect(page.getByText("Waiting for the first trade…")).toBeHidden({ timeout: 15_000 });

  await page.getByRole("button", { name: "4 charts" }).click();
  await expect(page.locator('[data-testid^="chart-c"]')).toHaveCount(4);
  for (const id of ["c1", "c2", "c3", "c4"]) {
    await expect(page.getByTestId(`chart-${id}`).locator("canvas").first()).toBeVisible();
  }

  const chart2 = page.getByRole("region", { name: "Chart c2" });
  await chart2.getByRole("button", { name: "15m" }).click();
  await expect(chart2.getByRole("button", { name: "15m" })).toHaveAttribute("aria-pressed", "true");

  await page.keyboard.press("Control+k");
  const palette = page.getByRole("dialog", { name: "Command palette" });
  await expect(palette).toBeVisible();
  await page.keyboard.type("1 chart");
  await expect(palette.getByRole("option")).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(palette).toBeHidden();
  await expect(page.locator('[data-testid^="chart-c"]')).toHaveCount(1);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/terminal-after-palette.png` });

  // settings persist across a reload
  await page.getByRole("button", { name: "2 charts" }).click();
  await page.reload();
  await expect(page.locator('[data-testid^="chart-c"]')).toHaveCount(2);
});
