import { describe, expect, it } from "vitest";

import { parseOrigin, publicLocation, route } from "../src/index";

const hosts = { app: "app.example.test", evidence: "evidence.example.test" };

describe("route", () => {
  it.each([
    // app.<domain>: /api and /api/* to api, everything else to web
    ["app.example.test", "/api", "api", "/api"],
    ["app.example.test", "/api/", "api", "/api/"],
    ["app.example.test", "/api/v1/threats", "api", "/api/v1/threats"],
    ["app.example.test", "/api/webhooks/postmark/inbound", "api", "/api/webhooks/postmark/inbound"],
    ["app.example.test", "/", "web", "/"],
    ["app.example.test", "/threats/123", "web", "/threats/123"],
    ["app.example.test", "/apis", "web", "/apis"],
    ["app.example.test", "/api-docs", "web", "/api-docs"],
    ["app.example.test", "/API/v1", "web", "/API/v1"],
    ["app.example.test", "/e/token", "web", "/e/token"],
    ["app.example.test", "//api/v1", "web", "//api/v1"],
    // evidence.<domain>: every path to api, unchanged
    ["evidence.example.test", "/", "api", "/"],
    ["evidence.example.test", "/e/abc", "api", "/e/abc"],
    ["evidence.example.test", "/e/abc/files/1", "api", "/e/abc/files/1"],
    ["evidence.example.test", "/api/v1/threats", "api", "/api/v1/threats"],
    // health aliases on either host go to /api/health on api (K80)
    ["app.example.test", "/healthz", "api", "/api/health"],
    ["app.example.test", "/api/healthz", "api", "/api/health"],
    ["evidence.example.test", "/healthz", "api", "/api/health"],
    ["evidence.example.test", "/api/healthz", "api", "/api/health"],
    // only the exact aliases are rewritten
    ["app.example.test", "/api/health", "api", "/api/health"],
    ["app.example.test", "/api/healthz/", "api", "/api/healthz/"],
    ["app.example.test", "/healthz/", "web", "/healthz/"],
    ["app.example.test", "/readyz", "web", "/readyz"],
    ["evidence.example.test", "/readyz", "api", "/readyz"],
    // host names compare case-insensitively
    ["APP.Example.Test", "/api/v1", "api", "/api/v1"],
  ])("%s%s -> %s %s", (host, path, upstream, upstreamPath) => {
    expect(route(host, path, hosts)).toEqual({ upstream, path: upstreamPath });
  });

  it.each([
    "example.test",
    "www.example.test",
    "app.example.test.evil.example",
    "evil-app.example.test",
    "kalkan-edge.workers.dev",
    "api-test.a.run.app",
    "",
  ])("serves no other host: %s", (host) => {
    expect(route(host, "/api/v1/threats", hosts)).toBeNull();
    expect(route(host, "/healthz", hosts)).toBeNull();
  });

  it("serves nothing when the host names are not configured", () => {
    expect(route("app.example.test", "/", { app: "", evidence: "" })).toBeNull();
    expect(route("", "/", { app: "", evidence: "" })).toBeNull();
  });
});

describe("parseOrigin", () => {
  it("accepts an https origin", () => {
    expect(parseOrigin("https://api-test.a.run.app")?.host).toBe("api-test.a.run.app");
    expect(parseOrigin("https://api-test.a.run.app/")?.host).toBe("api-test.a.run.app");
  });

  it.each([undefined, "", "api-test.a.run.app", "http://api-test.a.run.app", "ftp://x.example", "https://"])(
    "rejects %s",
    (value) => {
      expect(parseOrigin(value)).toBeNull();
    },
  );
});

describe("publicLocation", () => {
  const origins = ["api-test.a.run.app", "web-test.a.run.app"];

  it.each([
    ["https://api-test.a.run.app/api/v1/x/?a=1", "https://app.example.test/api/v1/x/?a=1"],
    ["http://api-test.a.run.app/api/v1/x/", "https://app.example.test/api/v1/x/"],
    ["https://web-test.a.run.app/", "https://app.example.test/"],
    ["//api-test.a.run.app/a%2Fb", "https://app.example.test/a%2Fb"],
  ])("moves %s to %s", (location, expected) => {
    expect(publicLocation(location, "app.example.test", origins)).toBe(expected);
  });

  it.each(["/x", "x", "", "https://example.com/x", "ftp://api-test.a.run.app/x", "https://[::1"])(
    "leaves %s alone",
    (location) => {
      expect(publicLocation(location, "app.example.test", origins)).toBeNull();
    },
  );

  it("matches nothing without configured origins", () => {
    expect(publicLocation("https://api-test.a.run.app/x", "app.example.test", [])).toBeNull();
  });
});
