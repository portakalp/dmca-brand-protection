import { expect, test } from "@playwright/test";

import { expectAccessible, noHorizontalOverflow } from "./support";

test("the threat list filters, sorts and pages through the URL", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto("/threats");
  await expect(page.getByText(/Showing 1–25 of \d+/)).toBeVisible();
  const total = Number(
    (await page.getByText(/Showing 1–25 of \d+/).textContent())!.split(" of ")[1],
  );
  expect(total).toBeGreaterThan(25);
  await page.screenshot({ path: "test-results/threats-1440.png", fullPage: true });
  await expectAccessible(page);

  await page.getByRole("combobox", { name: "Status" }).click();
  await page.getByRole("option", { name: "Taken down" }).click();
  await expect(page).toHaveURL(/status=taken_down/);
  await expect(page.getByRole("table").getByText("Taken down").first()).toBeVisible();
  const rows = page.getByRole("table").getByRole("row");
  for (const text of await rows.allTextContents()) {
    if (text.startsWith("Domain")) continue;
    expect(text).toContain("Taken down");
  }

  await page.getByRole("button", { name: /^Domain/ }).click();
  await expect(page).toHaveURL(/sort=domain/);

  await page.getByRole("searchbox", { name: "Search domains" }).fill("mintera");
  await expect(page).toHaveURL(/q=mintera/);

  // Reloading keeps every filter.
  await page.reload();
  await expect(page.getByRole("searchbox", { name: "Search domains" })).toHaveValue("mintera");
  await expect(page.getByRole("combobox", { name: "Status" })).toContainText("Taken down");

  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(page).not.toHaveURL(/status=|q=/);
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page).toHaveURL(/page=2/);
  await expect(page.getByText(/Showing 26–/)).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download CSV" }).click();
  expect((await download).suggestedFilename()).toMatch(/\.csv$/);
});

test("a row opens the threat and the list fits a phone", async ({ page }) => {
  await page.goto("/threats");
  const row = page.getByRole("table").getByRole("row").nth(1);
  const domain = (await row.getByRole("link").textContent())!.trim();
  await row.getByRole("cell").nth(3).click();
  await expect(page).toHaveURL(/\/threats\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { name: domain, level: 1 })).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/threats");
  await expect(page.getByRole("table")).toBeVisible();
  await noHorizontalOverflow(page);
});
