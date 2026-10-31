import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

import { asWritableOwner, expectAccessible, noHorizontalOverflow } from "./support";

/** The design's case (the seed's detail case), found through the list's search. */
async function openDesignCase(page: Page) {
  await page.goto("/threats?q=mintera-login.com");
  await page.getByRole("link", { name: "mintera-login.com", exact: true }).click();
  await expect(page).toHaveURL(/\/threats\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { name: "mintera-login.com", level: 1 })).toBeVisible();
}

async function imageLoaded(page: Page, name: RegExp) {
  const image = page.getByRole("img", { name });
  await expect
    .poll(() => image.evaluate((node: HTMLImageElement) => node.naturalWidth))
    .toBeGreaterThan(0);
  return image.getAttribute("src");
}

test("the threat detail matches the design and switches captures", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await openDesignCase(page);

  await expect(page.getByText("94%")).toBeVisible();
  await expect(
    page.getByRole("list", { name: "Matched signals" }).getByRole("listitem"),
  ).toHaveText(["Logo match", "Same page layout", "Identical favicon", "Brand name in domain"]);
  await expect(page.getByText("97% confidence", { exact: true })).toBeVisible();
  const timeline = page.getByRole("region", { name: "Timeline" });
  for (const step of [
    "Detected",
    "Captured",
    "Verified",
    "Reported to Cloudflare",
    "Host notified",
    "Waiting for takedown",
  ]) {
    await expect(timeline.getByText(step, { exact: true })).toBeVisible();
  }
  await expect(page.getByText("2 sent · 1 scheduled")).toBeVisible();
  await expect(page.getByText("5 files · 4.2 MB")).toBeVisible();
  await expect(page.getByText(/Stored unchanged until/)).toBeVisible();

  // The captures come from the API; the toggle swaps both sides.
  const desktop = await imageLoaded(page, /Desktop capture of the clone/);
  await page.screenshot({ path: "test-results/threat-detail-1440.png", fullPage: true });
  await expectAccessible(page);

  const toggle = page.getByRole("group", { name: "Capture" });
  await toggle.getByRole("button", { name: "Mobile" }).click();
  await expect(toggle.getByRole("button", { name: "Mobile" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  const mobile = await imageLoaded(page, /Mobile capture of the clone/);
  await imageLoaded(page, /Mobile capture of your site/);
  expect(mobile).not.toBe(desktop);
  await expect(page.getByText(/^Mobile capture · /)).toBeVisible();
});

test("the evidence downloads one by one and as a ZIP", async ({ page }) => {
  await openDesignCase(page);

  let download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download evidence" }).click();
  const zip = await download;
  expect(zip.suggestedFilename()).toMatch(/\.zip$/);
  const bytes = readFileSync((await zip.path())!);
  expect(bytes.subarray(0, 2).toString()).toBe("PK");

  download = page.waitForEvent("download");
  await page.getByRole("link", { name: /^Download rdap\.json/ }).click();
  // The API names the file as in the ZIP.
  expect((await download).suggestedFilename()).toMatch(/^rdap-[0-9a-f-]{36}\.json$/);
});

test("the threat detail stacks on a phone and reads in the dark", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openDesignCase(page);
  await imageLoaded(page, /Desktop capture of the clone/);
  await noHorizontalOverflow(page);
  await page.screenshot({ path: "test-results/threat-detail-390.png", fullPage: true });

  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.evaluate(() => localStorage.setItem("theme", "dark"));
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await imageLoaded(page, /Desktop capture of the clone/);
  await page.screenshot({ path: "test-results/threat-detail-1440-dark.png", fullPage: true });
  await expectAccessible(page);
  await page.evaluate(() => localStorage.setItem("theme", "light"));
});

test("an owner marks a threat as theirs after reading what happens", async ({ page }) => {
  await asWritableOwner(page);
  let sent: unknown = null;
  await page.route("**/api/v1/threats/*/mark-ours", async (route) => {
    sent = route.request().postDataJSON();
    await route.fulfill({
      json: {
        status: "queued",
        whitelist_entry: {
          id: "e2e",
          pattern: "mintera-login.com",
          kind: "mirror",
          note: "Our new domain",
          brand_id: null,
          source: "this_is_ours",
          created_by: "security@mintera.example",
          created_at: new Date().toISOString(),
        },
      },
    });
  });
  await openDesignCase(page);

  await page.getByRole("button", { name: "This is our site" }).click();
  const dialog = page.getByRole("dialog", { name: "Is mintera-login.com your site?" });
  await expect(dialog).toContainText("added to your whitelist");
  await expect(dialog).toContainText("This threat is dismissed");
  await expect(dialog).toContainText("the same recipients get a retraction e-mail");
  await expectAccessible(page);

  await dialog.getByRole("combobox", { name: "What is it?" }).click();
  await page.getByRole("option", { name: "Alternate domain" }).click();
  await dialog.getByLabel(/Note/).fill("Our new domain");
  await dialog.getByRole("button", { name: "Yes, it's our site" }).click();

  await expect(dialog).toBeHidden();
  await expect(page.getByText("mintera-login.com is on your whitelist")).toBeVisible();
  await expect(
    page.getByText("The threat is being dismissed and the notices withdrawn."),
  ).toBeVisible();
  expect(sent).toEqual({ kind: "mirror", note: "Our new domain" });
});

test("the demo owner sees the action but cannot use it", async ({ page }) => {
  await openDesignCase(page);
  await expect(page.getByRole("button", { name: "This is our site" })).toBeDisabled();
});
