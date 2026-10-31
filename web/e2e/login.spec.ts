import { expect, test } from "@playwright/test";

import { expectAccessible } from "./support";

test("pages need a session and a wrong password is refused", async ({ page }) => {
  await page.goto("/threats?status=active");
  await expect(page).toHaveURL(/\/login\?next=%2Fthreats%3Fstatus%3Dactive/);
  await expectAccessible(page);
  // An address with no account: the attempt limit of the demo owner stays untouched.
  await page.getByLabel("E-mail").fill("nobody@example.test");
  await page.getByLabel("Password").fill("not the password");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Wrong" })).toHaveText(
    "Wrong e-mail or password.",
  );
});

test("the invitation link of the e-mail opens the invitation page", async ({ page }) => {
  await page.goto("/invite?token=abc123");
  await expect(page).toHaveURL(/\/invite\/abc123$/);
  await expect(page.getByRole("heading", { name: "Join your team on Kalkan" })).toBeVisible();
  await expectAccessible(page);
});

test("a forgotten password gets a link, and a broken link says so", async ({ page }) => {
  // The request is answered here: no reset e-mail leaves the local stack.
  await page.route("**/api/v1/auth/password/forgot", (route) =>
    route.fulfill({ status: 202, body: "" }),
  );
  await page.goto("/login");
  await page.getByRole("link", { name: "Forgot password?" }).click();
  await expect(page).toHaveURL(/\/forgot-password$/);
  await expect(page.getByRole("heading", { name: "Reset your password" })).toBeVisible();
  await expectAccessible(page);
  await page.getByLabel("E-mail").fill("someone@mintera.example");
  await page.getByRole("button", { name: "Send the link" }).click();
  await expect(page.getByRole("heading", { name: "Check your e-mail" })).toBeVisible();

  await page.goto("/reset-password");
  await expect(page.getByRole("heading", { name: "This link doesn't work" })).toBeVisible();
  await page.goto("/reset-password?token=expired-link-for-the-test");
  await page.getByLabel("New password").fill("short");
  await page.getByRole("button", { name: "Save the password" }).click();
  await expect(page.getByText("Use at least 12 characters.").last()).toBeVisible();
  await expectAccessible(page);
});
