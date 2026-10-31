import { expect, test, type Page } from "@playwright/test";

import { expectAccessible, noHorizontalOverflow } from "./support";

function kpi(page: Page, title: string) {
  return page.getByRole("article").filter({ has: page.getByRole("heading", { name: title }) });
}

test("the overview shows the design's numbers and follows the date range", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto("/overview");
  await expect(kpi(page, "Detected").getByText("128", { exact: true })).toBeVisible();
  await expect(kpi(page, "Verified threats").getByText("47", { exact: true })).toBeVisible();
  await expect(kpi(page, "Taken down").getByText("39", { exact: true })).toBeVisible();
  await expect(kpi(page, "Taken down").getByText("83%")).toBeVisible();
  await expect(kpi(page, "Median takedown time")).toContainText("31h");
  await expect(kpi(page, "Median takedown time")).toContainText("22% faster");
  await expect(page.getByText("8 threats are still live.")).toBeVisible();
  await expect(page.getByRole("link", { name: /Threats/ }).first()).toContainText("8");
  const status = page.getByRole("region", { name: "Threat status" });
  await expect(status).toContainText("All 128 detections in this period");
  await expect(status).not.toContainText("Reviewing");
  const channels = page.getByRole("region", { name: "Takedowns by channel" });
  await expect(channels).toContainText("Cloudflare");
  await expect(channels).toContainText("17");
  await page.screenshot({ path: "test-results/overview-1440.png", fullPage: true });
  await expectAccessible(page);

  // The chart's 7 days narrows every number to the last week.
  await page
    .getByRole("group", { name: "Chart period" })
    .getByRole("button", { name: "7 days" })
    .click();
  await expect(page).toHaveURL(/range=7d/);
  await expect(page.getByRole("button", { name: "Date range: Last 7 days" })).toBeVisible();
  await expect(kpi(page, "Detected").getByText("128", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Threat status" })).not.toContainText(
    "All 128 detections",
  );

  // Back to 30 days from the header's date range.
  await page.getByRole("button", { name: "Date range: Last 7 days" }).click();
  await page.getByRole("button", { name: "Last 30 days" }).click();
  await expect(kpi(page, "Detected").getByText("128", { exact: true })).toBeVisible();
});

test("the overview fits a phone and reads in the dark theme", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/overview");
  await expect(kpi(page, "Detected").getByText("128", { exact: true })).toBeVisible();
  await noHorizontalOverflow(page);
  await page.screenshot({ path: "test-results/overview-390.png", fullPage: true });

  // The menu is a sheet on a phone.
  await page.getByRole("button", { name: "Open menu" }).click();
  await expect(page.getByRole("dialog").getByRole("link", { name: /Threats/ })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.evaluate(() => localStorage.setItem("theme", "dark"));
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect(kpi(page, "Detected").getByText("128", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/overview-1440-dark.png", fullPage: true });
  await expectAccessible(page);
});

test("a recent threat opens its detail route", async ({ page }) => {
  await page.goto("/overview");
  const recent = page.getByRole("region", { name: "Recent threats" });
  const first = recent.getByRole("row").nth(1);
  const domain = (await first.getByRole("link").textContent())!.trim();
  await first.getByRole("cell").nth(2).click();
  await expect(page).toHaveURL(/\/threats\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { name: domain, level: 1 })).toBeVisible();
});
