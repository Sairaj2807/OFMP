import { expect, type Page, test } from "@playwright/test";

import { EMAIL, PASSWORD } from "./env";

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const signedOut = { storageState: { cookies: [], origins: [] } };

// Tests use the session saved by auth.setup.ts unless they opt out.
async function openTerminal(page: Page) {
  await page.goto("/app/");
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  // the user's server-side workspace is applied asynchronously after sign-in: wait for it,
  // so it cannot overwrite the layout a test sets right after opening the terminal
  if ((await page.request.get("/api/v1/workspaces")).ok()) {
    await expect(page.getByRole("button", { name: /Workspace/ })).toContainText(/Saved|Saving/);
  }
}

// the page's own alert (Next.js adds a role=alert route announcer outside <main>)
const pageAlert = (page: Page) => page.locator("main").getByRole("alert");

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

test("account pages: legacy URLs redirect, forgot password, reset and verify links", async ({ page }) => {
  // old links (e.g. in emails sent before the legacy pages were retired) land on the terminal's pages
  await page.goto("/login");
  await page.waitForURL(/\/app\/login\/$/);
  await page.getByRole("button", { name: "Forgot password?" }).click();
  await expect(page.getByRole("heading", { name: "Reset password" })).toBeVisible();
  await page.getByLabel("Email").fill("nobody-e2e@example.com");
  await page.getByRole("button", { name: "Send reset link" }).click();
  await expect(page.locator("main").getByRole("status")).not.toBeEmpty();      // same answer whether or not the account exists
  await page.getByRole("button", { name: "Back to sign in" }).click();
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByLabel("Display name (optional)")).toBeVisible();

  await page.goto("/reset-password?token=not-a-real-token-123");
  await page.waitForURL(/\/app\/reset-password\/\?token=not-a-real-token-123$/);
  await page.getByLabel("New password", { exact: true }).fill("another password 9");
  await page.getByLabel("Confirm new password").fill("another password 9");
  await page.getByRole("button", { name: "Set password" }).click();
  await expect(pageAlert(page)).toContainText("Error:");
  await page.goto("/app/reset-password/");
  await expect(pageAlert(page)).toContainText("missing its token");

  await page.goto("/verify-email?token=not-a-real-token-456");
  await page.waitForURL(/\/app\/verify-email\/\?token=not-a-real-token-456$/);
  await expect(pageAlert(page)).toContainText("Error:");
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
  // layouts are saved per user on the server: set the state this test relies on
  await page.getByRole("button", { name: "1 chart" }).click();
  await page.getByRole("region", { name: "Chart c1" }).getByRole("button", { name: "Live", exact: true }).click();

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
  await page.getByRole("button", { name: "1 chart" }).click();
  await page.getByRole("region", { name: "Chart c1" }).getByRole("button", { name: "Live", exact: true }).click();
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

test("replay a recorded session: play, step and back to live", async ({ page }) => {
  const res = await page.request.get("/api/v1/market/replay/sessions");
  const sessions: { date: string }[] = res.ok() ? (await res.json()).data : [];
  test.skip(sessions.length === 0, "no recorded sessions on this backend");

  await openTerminal(page);
  await page.getByRole("button", { name: "1 chart" }).click();
  const chart = page.getByRole("region", { name: "Chart c1" });
  await chart.getByRole("button", { name: "Replay", exact: true }).click();
  const bar = chart.getByRole("group", { name: "Replay controls c1" });
  await expect(bar).toBeVisible();
  await expect(chart.getByLabel("Replay progress")).toHaveText(/^0 \//);

  await chart.getByRole("button", { name: "Step one trade" }).click();
  await expect(chart.getByLabel("Replay progress")).not.toHaveText(/^0 \//);
  await chart.getByLabel("Replay speed").selectOption("50");
  await chart.getByRole("button", { name: "Play replay" }).click();
  const before = await chart.getByLabel("Replay clock").textContent();
  await expect(chart.getByLabel("Replay clock")).not.toHaveText(before ?? "", { timeout: 5_000 });
  await expect(page.getByText("Waiting for the first trade…")).toBeHidden();
  await chart.getByRole("button", { name: "Pause replay" }).click();
  await expect(chart.getByRole("button", { name: "Play replay" })).toBeVisible();

  await chart.getByRole("button", { name: "Live", exact: true }).click();
  await expect(bar).toBeHidden();
});

test("workspaces: save as, autosave, persisted on the server, delete", async ({ page }) => {
  const probe = await page.request.get("/api/v1/workspaces");
  test.skip(probe.status() === 503, "workspaces need the database");
  await openTerminal(page);
  const menuButton = page.getByRole("button", { name: /Workspace/ });
  await expect(menuButton).toBeVisible();
  const name = `E2E ${Date.now()}`;

  await menuButton.click();
  await page.getByRole("menuitem", { name: "Save as…" }).click();
  await page.getByLabel("Workspace name").fill(name);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(menuButton).toContainText(name);

  await menuButton.click();
  await page.getByRole("menuitem", { name: "Make default" }).click();
  await page.getByRole("button", { name: "4 charts" }).click();
  await expect(menuButton).toContainText("Saved", { timeout: 5_000 });

  // server-side, not browser storage: clear local storage (layout and last-used) and reload;
  // the default workspace and its layout come back from the server
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page.getByRole("button", { name: /Workspace/ })).toContainText(name);
  await expect(page.locator('[data-testid^="chart-c"]')).toHaveCount(4);

  await page.getByRole("button", { name: /Workspace/ }).click();
  await page.getByRole("menuitem", { name: "Delete this workspace" }).click();
  await expect(page.getByRole("button", { name: /Workspace/ })).not.toContainText(name);
});

test("alerts: a price rule fires on live data, shows a toast and history, then is deleted", async ({ page }) => {
  const probe = await page.request.get("/api/v1/alerts/rules");
  test.skip(probe.status() === 503, "alerts need the database");
  test.setTimeout(90_000);
  // rules left by an earlier failed run would keep firing: remove them first
  const csrf = (await page.context().cookies()).find((c) => c.name === "ofmp_csrf")?.value ?? "";
  for (const r of (await probe.json()) as { id: string; name: string }[]) {
    if (r.name.startsWith("E2E alert")) {
      await page.request.delete(`/api/v1/alerts/rules/${r.id}`, { headers: { "X-CSRF-Token": csrf } });
    }
  }
  await openTerminal(page);
  await page.getByRole("button", { name: "1 chart" }).click();
  await page.getByRole("region", { name: "Chart c1" }).getByRole("button", { name: "Live", exact: true }).click();
  await expect(page.getByText("Waiting for the first trade…")).toBeHidden({ timeout: 15_000 });

  await page.getByRole("button", { name: /^Alerts/ }).click();
  const panel = page.getByRole("dialog", { name: "Alerts" });
  await panel.getByRole("tab", { name: "webhooks" }).click();
  await panel.getByLabel("Webhook name").fill("E2E hook");
  await panel.getByLabel("Webhook URL").fill("http://127.0.0.1:9/e2e");
  await panel.getByRole("button", { name: "Add" }).click();
  await expect(panel.getByLabel("Signing secret")).toHaveText(/^[0-9a-f]{64}$/);
  await panel.getByRole("button", { name: "I have copied it" }).click();
  await panel.getByRole("button", { name: "Delete E2E hook" }).click();
  await expect(panel.getByText("E2E hook")).toBeHidden();

  // a rule at the current price: the live feed crosses it within seconds
  const name = `E2E alert ${Date.now()}`;
  await panel.getByRole("tab", { name: "rules" }).click();
  await panel.getByRole("button", { name: "New alert" }).click();
  const form = panel.getByRole("form", { name: "New alert" });
  await form.getByLabel("Name").fill(name);
  await expect(form.getByLabel("Level")).not.toHaveValue("");          // prefilled with the last price
  await form.getByLabel("At most every (seconds)").fill("10");
  await form.getByRole("button", { name: "Create alert" }).click();
  await expect(panel.getByRole("listitem", { name: `Rule ${name}` })).toBeVisible();
  await page.keyboard.press("Escape");

  const toast = page.getByLabel("New alerts").getByRole("status").filter({ hasText: name });
  await expect(toast.first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("button", { name: /^Alerts, \d+ unread/ })).toBeVisible();
  await toast.first().getByRole("button", { name: "Open alerts" }).click();
  await expect(panel.getByRole("list", { name: "Alert history" })).toContainText(name);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/alerts.png` });
  await panel.getByRole("button", { name: "Mark all read" }).click();
  await expect(page.getByRole("button", { name: "Alerts", exact: true })).toBeVisible();

  await panel.getByRole("tab", { name: "rules" }).click();
  await panel.getByRole("button", { name: `Delete ${name}` }).click();
  await expect(panel.getByRole("listitem", { name: `Rule ${name}` })).toBeHidden();
});
