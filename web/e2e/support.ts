import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/** The signed-in users the panel tests reuse (playwright.config.ts). */
export const OWNER_STATE = "e2e/.auth/owner.json";
export const VIEWER_STATE = "e2e/.auth/viewer.json";

export type DemoUser = {
  role: "owner" | "viewer";
  email: string;
  password: string;
  totp_secret: string;
};

/** The demo users `make seed` writes outside git (.local/demo-credentials.json). */
export function demoUser(role: DemoUser["role"] = "owner"): DemoUser {
  const file = resolve(__dirname, "../../.local/demo-credentials.json");
  const users = JSON.parse(readFileSync(file, "utf8")).users as DemoUser[];
  const user = users.find((candidate) => candidate.role === role);
  if (!user) throw new Error(`No ${role} in ${file}; run make seed`);
  return user;
}

function base32(secret: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  }
  const bytes = bits.match(/.{8}/g) ?? [];
  return Buffer.from(bytes.map((byte) => Number.parseInt(byte, 2)));
}

/** The current RFC 6238 code (SHA-1, 30 seconds, 6 digits), as an authenticator app shows. */
export function totp(secret: string, at = Date.now()): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 30_000)));
  const digest = createHmac("sha1", base32(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const value = (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return value.toString().padStart(6, "0");
}

/** No serious or critical axe violation. */
export async function expectAccessible(page: Page) {
  // Measure the settled page: a button fading in from disabled is not a contrast problem.
  await page.addStyleTag({
    content: "*, *::before, *::after { transition: none !important; animation: none !important; }",
  });
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  const serious = results.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );
  expect(
    serious.map(
      (violation) =>
        `${violation.id}: ${violation.nodes.map((n) => `${n.target} (${n.failureSummary})`).join(", ")}`,
    ),
  ).toEqual([]);
}

export async function noHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/**
 * The demo workspace is read-only, so the write flows run against a writable copy of it in the
 * browser: /me says the owner may write and the write endpoints answer from the test (K160). The
 * API's own refusals are tested against the real API (roles.spec.ts) and in the backend.
 */
export async function asWritableOwner(page: Page) {
  await page.route("**/api/v1/me", async (route) => {
    const response = await route.fetch();
    const me = await response.json();
    me.customer = { ...me.customer, read_only: false, is_demo: false, status: "active" };
    me.permissions = {
      write_whitelist: true,
      mark_ours: true,
      manage_users: true,
      settings: true,
    };
    await route.fulfill({ response, json: me });
  });
}

type Entry = Record<string, unknown> & { id: string; pattern: string };

/** A whitelist kept in the test: GET, POST, DELETE and the CSV import, with dry runs. */
export async function mockWhitelist(page: Page) {
  let entries: Entry[] | null = null;
  const writes: { method: string; url: string; body: unknown }[] = [];
  await page.route(/\/api\/v1\/whitelist(\/[^?]*)?(\?.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (url.pathname === "/api/v1/whitelist/export") return route.fallback();
    if (entries === null) {
      entries = (await (await page.request.get("/api/v1/whitelist")).json()) as Entry[];
    }
    if (method === "GET") return route.fulfill({ json: entries });
    const body = request.headers()["content-type"]?.includes("json")
      ? request.postDataJSON()
      : null;
    writes.push({ method, url: url.pathname + url.search, body });
    if (url.pathname === "/api/v1/whitelist" && method === "POST") {
      if (body.pattern === "github.io") {
        return route.fulfill({
          status: 422,
          contentType: "application/problem+json",
          json: {
            status: 422,
            title: "This host cannot be whitelisted",
            detail: "github.io is a shared platform; add your own subdomain instead.",
          },
        });
      }
      const entry: Entry = {
        id: `e2e-${entries.length}`,
        pattern: String(body.pattern).toLowerCase(),
        kind: body.kind,
        note: body.note,
        brand_id: null,
        source: "manual",
        created_by: "security@mintera.example",
        created_at: new Date().toISOString(),
      };
      entries = [entry, ...entries];
      return route.fulfill({ status: 201, json: entry });
    }
    if (url.pathname === "/api/v1/whitelist/import") {
      const result = {
        added: 2,
        skipped: 1,
        errors: [{ line: 4, value: "com", error: "com is a public suffix" }],
      };
      if (url.searchParams.get("dry_run") !== "true") {
        entries = [
          ...["imported-one.example", "imported-two.example"].map((pattern, index) => ({
            id: `csv-${index}`,
            pattern,
            kind: "partner",
            note: null,
            brand_id: null,
            source: "csv",
            created_by: "security@mintera.example",
            created_at: new Date().toISOString(),
          })),
          ...entries,
        ];
      }
      return route.fulfill({ json: result });
    }
    if (method === "DELETE") {
      const id = url.pathname.split("/").pop();
      entries = entries.filter((entry) => entry.id !== id);
      return route.fulfill({ status: 204, body: "" });
    }
    return route.fallback();
  });
  return writes;
}
