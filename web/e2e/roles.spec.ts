import { expect, test } from "@playwright/test";

import { expectAccessible } from "./support";

const CSRF = { "X-Kalkan-CSRF": "1" };

test("a viewer sees no write buttons", async ({ page }) => {
  await page.goto("/threats?q=mintera-login.com");
  await page.getByRole("link", { name: "mintera-login.com", exact: true }).click();
  await expect(page.getByRole("heading", { name: "mintera-login.com", level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: "Download evidence" })).toBeVisible();
  await expect(page.getByRole("button", { name: "This is our site" })).toHaveCount(0);

  await page.goto("/whitelist");
  await expect(page.getByText("24 domains")).toBeVisible();
  for (const name of ["Add domain", "Import CSV"]) {
    await expect(page.getByRole("button", { name })).toHaveCount(0);
  }
  await expect(page.getByRole("link", { name: "Export CSV" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Remove / })).toHaveCount(0);
  await expectAccessible(page);

  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Your account" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Users" })).toHaveCount(0);
  await expect(page.getByText("Only owners manage users")).toBeVisible();
});

test("the API refuses a viewer's writes too", async ({ page }) => {
  const list = await page.request.get("/api/v1/threats?q=mintera-login.com");
  const threat = (await list.json()).items[0];
  const refusals = [
    await page.request.post("/api/v1/whitelist", {
      headers: CSRF,
      data: { pattern: "viewer-test.example" },
    }),
    await page.request.post(`/api/v1/threats/${threat.id}/mark-ours`, { headers: CSRF, data: {} }),
    await page.request.post("/api/v1/users", {
      headers: CSRF,
      data: { email: "someone@mintera.example" },
    }),
    await page.request.get("/api/v1/whitelist/export"),
  ];
  for (const response of refusals) {
    expect(response.status()).toBe(403);
    expect((await response.json()).code).toBe("role");
  }
});
