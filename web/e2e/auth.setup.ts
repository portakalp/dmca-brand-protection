import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

import { expect, test as setup, type Page } from "@playwright/test";

import { OWNER_STATE, VIEWER_STATE, demoUser, totp } from "./support";

for (const [role, state] of [
  ["owner", OWNER_STATE],
  ["viewer", VIEWER_STATE],
] as const) {
  setup(`the demo ${role} signs in with password and TOTP`, async ({ page }) => {
    await signIn(page, role, state);
  });
}

async function signIn(page: Page, role: "owner" | "viewer", state: string) {
  const user = demoUser(role);
  await page.goto("/overview");
  await expect(page).toHaveURL(/\/login\?next=%2Foverview/);
  await page.getByLabel("E-mail").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page).toHaveURL(/\/login\/mfa/);
  await page.getByLabel("Authentication code").fill(totp(user.totp_secret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/overview$/);
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  mkdirSync(dirname(state), { recursive: true });
  await page.context().storageState({ path: state });
}
