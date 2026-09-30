import { expect, test as setup } from "@playwright/test";

import { AUTH_FILE, EMAIL, PASSWORD } from "./env";

// Sign in once per run and share the session with the tests (login is rate
// limited per account, as it should be).
setup("sign in", async ({ page }) => {
  await page.goto("/app/login/");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/app\/$/);
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await page.context().storageState({ path: AUTH_FILE });
});
