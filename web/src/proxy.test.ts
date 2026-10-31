import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isPublic, proxy } from "./proxy";

function request(path: string, headers: Record<string, string> = {}) {
  return new NextRequest(new URL(path, "https://app.example.test"), { headers });
}

afterEach(() => vi.unstubAllEnvs());

describe("proxy", () => {
  it("sends a visitor without a session to the sign-in page", () => {
    const response = proxy(request("/threats?status=active"));
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe(
      "https://app.example.test/login?next=%2Fthreats%3Fstatus%3Dactive",
    );
    expect(proxy(request("/")).headers.get("location")).toBe("https://app.example.test/login");
  });

  it("lets the session cookie and the sign-in pages through", () => {
    expect(proxy(request("/overview", { cookie: "kalkan_session=abc" })).status).toBe(200);
    expect(proxy(request("/login")).status).toBe(200);
    expect(proxy(request("/invite/abc")).status).toBe(200);
    expect(proxy(request("/icon.svg")).status).toBe(200);
    expect(proxy(request("/forgot-password")).status).toBe(200);
    expect(proxy(request("/reset-password?token=abc")).status).toBe(200);
    expect(isPublic("/loginx")).toBe(false);
  });

  it("moves the e-mail's invitation link to its route", () => {
    expect(proxy(request("/invite?token=a%2Fb")).headers.get("location")).toBe(
      "https://app.example.test/invite/a%2Fb",
    );
  });

  it("lets the print page through with a report token instead of the cookie", () => {
    const page = "/print/report/4f9d6f0e-5d41-4c55-9f1d-8a6f1f9b2c11";
    expect(proxy(request(page)).status).toBe(401);
    const printed = proxy(request(page, { "x-report-token": "t" }));
    expect(printed.status).toBe(200);
    expect(printed.headers.get("cache-control")).toBe("no-store");
    expect(proxy(request("/print/other", { "x-report-token": "t" })).status).toBe(404);
    expect(proxy(request(`${page}/x`, { "x-report-token": "t" })).status).toBe(404);
    // A session cookie does not open it either.
    expect(proxy(request(page, { cookie: "kalkan_session=abc" })).status).toBe(401);
  });

  it("refuses requests that skipped the edge Worker", () => {
    vi.stubEnv("EDGE_SHARED_SECRET", "s3cret");
    expect(proxy(request("/login")).status).toBe(403);
    expect(proxy(request("/login", { "x-edge-auth": "wrong" })).status).toBe(403);
    expect(proxy(request("/login", { "x-edge-auth": "s3cret" })).status).toBe(200);
    expect(proxy(request("/print/report/x", { "x-report-token": "t" })).status).toBe(403);
  });
});
