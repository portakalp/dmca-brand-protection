import { expect, test } from "@playwright/test";

import { asWritableOwner, expectAccessible, mockWhitelist, noHorizontalOverflow } from "./support";

test("the demo owner reads and exports the whitelist; writes are off", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/whitelist");
  const table = page.getByRole("table", { name: "Whitelisted domains" });
  await expect(table.getByText("mintera.com", { exact: true })).toBeVisible();
  await expect(page.getByText("24 domains")).toBeVisible();
  await expect(page.getByRole("button", { name: "Add domain" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Import CSV" })).toBeDisabled();
  await page.screenshot({ path: "test-results/whitelist-1440.png", fullPage: true });
  await expectAccessible(page);

  await page.getByRole("searchbox", { name: "Search the whitelist" }).fill("medium");
  await expect(page.getByText("1 of 24 domains")).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Export CSV" }).click();
  expect((await download).suggestedFilename()).toBe("whitelist.csv");

  await page.setViewportSize({ width: 390, height: 844 });
  await noHorizontalOverflow(page);
});

test("an owner adds a domain, sees the API's refusal and removes it", async ({ page }) => {
  await asWritableOwner(page);
  const writes = await mockWhitelist(page);
  await page.goto("/whitelist");

  await page.getByRole("button", { name: "Add domain" }).click();
  const dialog = page.getByRole("dialog", { name: "Add a domain" });
  await dialog.getByRole("button", { name: "Add domain" }).click();
  await expect(dialog.getByText("Enter a domain.")).toBeVisible();

  await dialog.getByLabel("Domain").fill("github.io");
  await dialog.getByRole("button", { name: "Add domain" }).click();
  await expect(dialog.getByText(/github\.io is a shared platform/)).toBeVisible();
  await expectAccessible(page);

  await dialog.getByLabel("Domain").fill("Partner-New.example");
  await dialog.getByRole("combobox", { name: "Kind" }).click();
  await page.getByRole("option", { name: "Partner" }).click();
  await dialog.getByRole("button", { name: "Add domain" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("partner-new.example is on your whitelist")).toBeVisible();
  const table = page.getByRole("table", { name: "Whitelisted domains" });
  await expect(table.getByText("partner-new.example", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Remove partner-new.example" }).click();
  const confirm = page.getByRole("alertdialog", { name: "Remove partner-new.example?" });
  await expect(confirm).toContainText("may report this domain again");
  await confirm.getByRole("button", { name: "Remove" }).click();
  await expect(table.getByText("partner-new.example", { exact: true })).toBeHidden();

  expect(writes.map((write) => write.method)).toEqual(["POST", "POST", "DELETE"]);
  expect(writes[1].body).toMatchObject({ pattern: "Partner-New.example", kind: "partner" });
});

test("an owner previews a CSV import with its row errors, then imports", async ({ page }) => {
  await asWritableOwner(page);
  const writes = await mockWhitelist(page);
  await page.goto("/whitelist");

  await page.getByRole("button", { name: "Import CSV" }).click();
  const dialog = page.getByRole("dialog", { name: "Import domains from a CSV file" });
  await dialog.locator("#whitelist-csv").setInputFiles({
    name: "partners.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "pattern,kind,note\nimported-one.example,partner,\nimported-two.example,partner,\ncom,other,\nmintera.com,official,\n",
    ),
  });
  await expect(dialog.getByRole("status")).toHaveText(
    "2 to add · 1 already on the list · 1 with errors",
  );
  const rows = dialog.getByRole("table", { name: "Rows in the file" }).getByRole("row");
  await expect(rows).toHaveCount(5);
  await expect(rows.nth(3)).toContainText("com is a public suffix");
  await expectAccessible(page);
  expect(writes.at(-1)?.url).toContain("dry_run=true");

  await dialog.getByRole("button", { name: "Import 2 domains" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("2 domains added to your whitelist")).toBeVisible();
  await expect(page.getByText("1 row was skipped because of errors.")).toBeVisible();
  await expect(
    page.getByRole("table", { name: "Whitelisted domains" }).getByText("imported-one.example"),
  ).toBeVisible();
  expect(writes.at(-1)?.url).toContain("dry_run=false");
});
