import { expect, test } from "@playwright/test";

import { expectAccessible, noHorizontalOverflow } from "./support";

test("reports and settings render for the demo owner", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });

  await page.goto("/reports");
  // The seed's last month: its CSV is real, the PDF is printed by worker-browser only.
  const reports = page.getByRole("table", { name: "Monthly reports" });
  const csv = reports.getByRole("link", { name: /report as CSV$/ });
  await expect(csv).toHaveCount(1);
  await expect(reports.getByRole("link", { name: /report as PDF$/ })).toHaveCount(0);
  const download = await page.request.get((await csv.getAttribute("href"))!);
  expect(download.status()).toBe(200);
  expect(download.headers()["content-disposition"]).toMatch(
    /^attachment; filename="report-\d{4}-\d{2}\.csv"$/,
  );
  expect((await download.text()).split("\r\n")[0]).toBe(
    "number,domain,category,status,first_seen_at,first_notice_at,taken_down_at,takedown_hours,channel",
  );
  await page.screenshot({ path: "test-results/reports-1440.png", fullPage: true });
  await expectAccessible(page);

  await page.goto("/settings");
  await expect(page.getByRole("table", { name: "Users and invitations" })).toContainText(
    "analyst@mintera.example",
  );
  // The demo is read-only: the controls are there but off.
  await expect(page.getByRole("button", { name: "Invite" })).toBeDisabled();
  await expect(page.getByLabel("Alert e-mail")).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Disable analyst@mintera.example" }),
  ).toBeDisabled();
  await page.screenshot({ path: "test-results/settings-1440.png", fullPage: true });
  await expectAccessible(page);

  await page.getByRole("button", { name: "New recovery codes" }).click();
  const dialog = page.getByRole("dialog", { name: "Get new recovery codes" });
  await expect(dialog.getByRole("button", { name: "Get new codes" })).toBeDisabled();
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await page.setViewportSize({ width: 390, height: 844 });
  await noHorizontalOverflow(page);
});

test("the demo workspace refuses writes in the API as well", async ({ page }) => {
  const response = await page.request.post("/api/v1/whitelist", {
    headers: { "X-Kalkan-CSRF": "1" },
    data: { pattern: "demo-test.example" },
  });
  expect(response.status()).toBe(403);
  expect((await response.json()).code).toBe("read_only");
});

test("a suspended owner sees why changes are turned off", async ({ page }) => {
  await page.route("**/api/v1/me", async (route) => {
    const response = await route.fetch();
    const me = await response.json();
    me.customer = { ...me.customer, is_demo: false, status: "suspended", read_only: true };
    await route.fulfill({ response, json: me });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/settings");
  await expect(page.getByRole("status").getByText("This account is suspended.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Invite" })).toBeDisabled();
  await expectAccessible(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await noHorizontalOverflow(page);
});
