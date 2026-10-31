import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import { expectAccessible } from "./support";

type DemoReport = { id: string; period: string; print_token: string };

/** Last month's demo report and its single-use print token (`make seed`, §21). */
function demoReport(): DemoReport {
  const file = resolve(__dirname, "../../.local/demo-credentials.json");
  const report = JSON.parse(readFileSync(file, "utf8")).report as DemoReport | undefined;
  if (!report) throw new Error(`No report in ${file}; run make seed`);
  return report;
}

const PAGE = /\/Type\s*\/Page(?!s)/g;

test("the print page prints the report once with its token and never without", async ({ page }) => {
  const report = demoReport();
  const path = `/print/report/${report.id}`;

  // No token, no page; the session cookie does not open it either (this project has none).
  expect((await page.request.get(path)).status()).toBe(401);

  await page.setExtraHTTPHeaders({ "x-report-token": report.print_token });
  await page.setViewportSize({ width: 794, height: 1123 });
  await page.goto(path);
  await page.locator("html[data-report-ready]").waitFor({ state: "attached" });
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Threats taken down" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "How the numbers are counted" })).toBeVisible();
  await expect(page.locator("body")).not.toContainText(report.print_token);
  await page.screenshot({ path: "test-results/report-print.png", fullPage: true });
  await expectAccessible(page);

  // What worker-browser does with the page (A4, backgrounds on).
  const pdf = await page.pdf({ format: "A4", printBackground: true, preferCSSPageSize: true });
  writeFileSync("test-results/report-sample.pdf", pdf);
  const pages = pdf.toString("latin1").match(PAGE)?.length ?? 0;
  expect(pages).toBeGreaterThanOrEqual(2);
  expect(pages).toBeLessThanOrEqual(4);

  // The token is spent: the same request gets no report.
  await page.goto(path);
  await expect(page.getByText("This report is not available.")).toBeVisible();
  await expect(page.locator("html[data-report-ready]")).toHaveCount(0);
});
