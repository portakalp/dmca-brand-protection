import { env, exports } from "cloudflare:workers";
import { describe, expect, it } from "vitest";

import { handle } from "../src/index";
import { mockUpstream } from "./upstream";

// Bindings come from vitest.config.ts.
const API = "https://api-test.a.run.app";
const WEB = "https://web-test.a.run.app";
const SECRET = "test-edge-secret";

describe("routing through the Worker", () => {
  it.each([
    ["https://app.example.test/api/v1/threats", `${API}/api/v1/threats`],
    ["https://app.example.test/api", `${API}/api`],
    ["https://app.example.test/threats/123", `${WEB}/threats/123`],
    ["https://app.example.test/", `${WEB}/`],
    ["https://evidence.example.test/e/tok3n", `${API}/e/tok3n`],
    ["https://evidence.example.test/e/tok3n/files/42", `${API}/e/tok3n/files/42`],
  ])("%s -> %s", async (publicUrl, upstreamUrl) => {
    const sent = mockUpstream();
    const response = await exports.default.fetch(publicUrl);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("upstream ok");
    expect(sent).toHaveLength(1);
    expect(sent[0]?.url).toBe(upstreamUrl);
  });

  it.each([
    "https://app.example.test/healthz",
    "https://app.example.test/api/healthz",
    "https://evidence.example.test/healthz",
    "https://evidence.example.test/api/healthz",
  ])("sends the health alias %s to api /api/health with the edge header", async (publicUrl) => {
    const sent = mockUpstream(() => Response.json({ status: "ok" }));
    const response = await exports.default.fetch(publicUrl);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
    expect(sent[0]?.url).toBe(`${API}/api/health`);
    expect(sent[0]?.headers.get("x-edge-auth")).toBe(SECRET);
  });

  it("keeps the query on a health alias", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://app.example.test/api/healthz?probe=deploy");
    expect(sent[0]?.url).toBe(`${API}/api/health?probe=deploy`);
  });

  it.each([
    "https://example.test/api/v1/threats",
    "https://www.example.test/",
    "https://api-test.a.run.app/api/v1/threats",
    "https://kalkan-edge.workers.dev/healthz",
  ])("answers 404 for another host and contacts nothing: %s", async (publicUrl) => {
    const sent = mockUpstream();
    const response = await exports.default.fetch(publicUrl);
    expect(response.status).toBe(404);
    expect(sent).toHaveLength(0);
  });

  it("keeps a protocol-relative looking path on the origin", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://app.example.test//evil.example/x");
    await exports.default.fetch("https://evidence.example.test//evil.example/x");
    expect(sent.map((request) => request.url)).toEqual([
      `${WEB}//evil.example/x`,
      `${API}//evil.example/x`,
    ]);
  });

  it("redirects plain HTTP to HTTPS without contacting the origin", async () => {
    const sent = mockUpstream();
    const response = await exports.default.fetch("http://app.example.test/api/v1/me?x=1", {
      method: "POST",
      body: "{}",
      redirect: "manual", // the test client must not follow it either
    });
    expect(response.status).toBe(308);
    expect(response.headers.get("location")).toBe("https://app.example.test/api/v1/me?x=1");
    expect(sent).toHaveLength(0);
  });
});

describe("headers", () => {
  it("injects the edge headers and overwrites spoofed ones", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://app.example.test/api/v1/threats", {
      headers: {
        "cf-connecting-ip": "203.0.113.7",
        "x-edge-auth": "spoofed-secret",
        "x-forwarded-for": "198.51.100.66, 10.0.0.1",
        "x-forwarded-host": "evil.example",
        "x-forwarded-proto": "http",
        forwarded: "for=198.51.100.66;host=evil.example;proto=http",
      },
    });
    const headers = sent[0]?.headers;
    expect(headers?.get("x-edge-auth")).toBe(SECRET);
    expect(headers?.get("x-forwarded-host")).toBe("app.example.test");
    expect(headers?.get("x-forwarded-for")).toBe("203.0.113.7");
    expect(headers?.get("x-forwarded-proto")).toBe("https");
    expect(headers?.has("forwarded")).toBe(false);
    expect(headers?.has("host")).toBe(false);
  });

  it("sets X-Forwarded-Host to the public host the request came in on", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://evidence.example.test/e/tok3n");
    expect(sent[0]?.headers.get("x-forwarded-host")).toBe("evidence.example.test");
  });

  it("drops a client X-Forwarded-For when Cloudflare gave no client IP", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://app.example.test/api/v1/threats", {
      headers: { "x-forwarded-for": "198.51.100.66" },
    });
    expect(sent[0]?.headers.has("x-forwarded-for")).toBe(false);
  });

  it("passes the other request headers through", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://app.example.test/api/v1/threats", {
      headers: {
        cookie: "kalkan_session=abc",
        "x-kalkan-csrf": "1",
        "x-request-id": "req-1",
        "user-agent": "test-agent",
        "accept-language": "tr",
      },
    });
    const headers = sent[0]?.headers;
    expect(headers?.get("cookie")).toBe("kalkan_session=abc");
    expect(headers?.get("x-kalkan-csrf")).toBe("1");
    expect(headers?.get("x-request-id")).toBe("req-1");
    expect(headers?.get("user-agent")).toBe("test-agent");
    expect(headers?.get("accept-language")).toBe("tr");
  });

  it("adds the edge header on web requests too", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://app.example.test/login", {
      headers: { "x-edge-auth": "spoofed-secret" },
    });
    expect(sent[0]?.url).toBe(`${WEB}/login`);
    expect(sent[0]?.headers.get("x-edge-auth")).toBe(SECRET);
  });
});

describe("pass-through", () => {
  it("sends method, query and a binary body byte for byte", async () => {
    const sent = mockUpstream();
    const body = new Uint8Array(256).map((_, index) => index); // includes bytes that are not UTF-8
    const query = "?a=1&b=%2F%20x&a=2&empty=&flag&t%C3%BCr=%C3%A7";
    await exports.default.fetch(`https://app.example.test/api/webhooks/postmark/bounce${query}`, {
      method: "POST",
      headers: { "content-type": "application/octet-stream" },
      body,
    });
    expect(sent[0]?.method).toBe("POST");
    expect(sent[0]?.url).toBe(`${API}/api/webhooks/postmark/bounce${query}`);
    expect(sent[0]?.headers.get("content-type")).toBe("application/octet-stream");
    expect(sent[0]?.body).toEqual(body);
  });

  it.each(["PUT", "PATCH", "DELETE"])("passes %s with its JSON body", async (method) => {
    const sent = mockUpstream();
    const json = '{"note":"çğış","n":1}';
    await exports.default.fetch("https://app.example.test/api/v1/whitelist/7", {
      method,
      headers: { "content-type": "application/json" },
      body: json,
    });
    expect(sent[0]?.method).toBe(method);
    expect(new TextDecoder().decode(sent[0]?.body ?? undefined)).toBe(json);
  });

  it("sends GET and HEAD without a body", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://app.example.test/api/v1/threats");
    await exports.default.fetch("https://app.example.test/api/v1/threats", { method: "HEAD" });
    expect(sent.map((request) => [request.method, request.body])).toEqual([
      ["GET", null],
      ["HEAD", null],
    ]);
  });

  it("keeps percent-encoding in the path", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://app.example.test/api/v1/threats/a%2Fb%20c");
    expect(sent[0]?.url).toBe(`${API}/api/v1/threats/a%2Fb%20c`);
  });

  it("does not follow redirects and returns them as is", async () => {
    const sent = mockUpstream(
      () => new Response(null, { status: 302, headers: { location: "/api/v1/login" } }),
    );
    const response = await exports.default.fetch("https://app.example.test/api/v1/me", {
      redirect: "manual", // the test client must not follow it either
    });
    expect(sent).toHaveLength(1);
    expect(sent[0]?.redirect).toBe("manual");
    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe("/api/v1/login");
  });

  it("bypasses Cloudflare's cache", async () => {
    const sent = mockUpstream();
    await exports.default.fetch("https://evidence.example.test/e/tok3n/files/42.png");
    expect(sent[0]?.cache).toBe("no-store");
  });

  it("returns the origin response as is", async () => {
    mockUpstream(
      () =>
        new Response(new Uint8Array([0, 255, 1, 254]), {
          status: 418,
          headers: {
            "content-type": "application/octet-stream",
            "set-cookie": "kalkan_session=xyz; Secure; HttpOnly",
            "x-request-id": "req-2",
          },
        }),
    );
    const response = await exports.default.fetch("https://app.example.test/api/v1/me");
    expect(response.status).toBe(418);
    expect(response.headers.get("content-type")).toBe("application/octet-stream");
    expect(response.headers.get("set-cookie")).toBe("kalkan_session=xyz; Secure; HttpOnly");
    expect(response.headers.get("x-request-id")).toBe("req-2");
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(new Uint8Array([0, 255, 1, 254]));
  });

  it("answers 502 when the origin cannot be reached", async () => {
    // test/setup.ts makes every unmocked fetch fail.
    const response = await exports.default.fetch("https://app.example.test/api/v1/me");
    expect(response.status).toBe(502);
  });
});

describe("redirect Location", () => {
  const redirectTo = (location: string) =>
    mockUpstream(() => new Response(null, { status: 307, headers: { location } }));
  const fetchManual = (publicUrl: string) =>
    exports.default.fetch(publicUrl, { redirect: "manual" }); // the test client must not follow it

  const APP = "https://app.example.test";
  const EVIDENCE = "https://evidence.example.test";

  it.each([
    // FastAPI's redirect_slashes answer as Cloud Run sees it: uvicorn gets plain http.
    [
      `${APP}/api/v1/threats?page=2`,
      "http://api-test.a.run.app/api/v1/threats/?page=2",
      `${APP}/api/v1/threats/?page=2`,
    ],
    [`${APP}/api/v1/me`, `${API}/api/v1/me/`, `${APP}/api/v1/me/`],
    [`${APP}/api/v1/me`, "//api-test.a.run.app/api/v1/me/", `${APP}/api/v1/me/`],
    [`${APP}/api/v1/me`, "https://API-TEST.a.run.app:443/x#y", `${APP}/x#y`],
    [`${APP}/threats`, `${WEB}/login?next=%2Fthreats`, `${APP}/login?next=%2Fthreats`],
    [`${EVIDENCE}/e/tok3n`, `${API}/e/tok3n/`, `${EVIDENCE}/e/tok3n/`],
  ])("moves an origin Location to the public host: %s, %s", async (from, location, expected) => {
    redirectTo(location);
    const response = await fetchManual(from);
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe(expected);
  });

  it.each([
    "/api/v1/login",
    "login?next=%2F",
    "https://app.example.test/api/v1/login",
    "https://accounts.example.com/authorize?redirect_uri=https%3A%2F%2Fapi-test.a.run.app%2F",
    "https://other-service.a.run.app/api/v1/me",
    "https://api-test.a.run.app:8443/api/v1/me",
    "https://api-test.a.run.app.evil.example/api/v1/me",
    "mailto:ops@example.test",
  ])("leaves any other Location untouched: %s", async (location) => {
    redirectTo(location);
    const response = await fetchManual("https://app.example.test/api/v1/me");
    expect(response.headers.get("location")).toBe(location);
  });

  it("keeps status, the other headers and the body when it rewrites", async () => {
    mockUpstream(
      () =>
        new Response("moved", {
          status: 308,
          headers: {
            location: `${API}/api/v1/threats/`,
            "set-cookie": "kalkan_session=xyz; Secure; HttpOnly",
            "x-request-id": "req-3",
          },
        }),
    );
    const response = await fetchManual("https://app.example.test/api/v1/threats");
    expect(response.status).toBe(308);
    expect(response.headers.get("location")).toBe("https://app.example.test/api/v1/threats/");
    expect(response.headers.get("set-cookie")).toBe("kalkan_session=xyz; Secure; HttpOnly");
    expect(response.headers.get("x-request-id")).toBe("req-3");
    expect(await response.text()).toBe("moved");
  });

  it("does not treat an unset web origin as a match", async () => {
    mockUpstream(() => new Response(null, { status: 302, headers: { location: `${WEB}/login` } }));
    const response = await handle(new Request("https://app.example.test/api/v1/me"), {
      ...env,
      WEB_ORIGIN: "",
    });
    expect(response.headers.get("location")).toBe(`${WEB}/login`);
  });
});

describe("fail closed", () => {
  // Unit-style: the handler is called directly so each test can change the environment.
  const call = (path: string, overrides: Partial<Env>) =>
    handle(new Request(`https://${path}`), { ...env, ...overrides });

  it("answers 503 for web paths while web is not deployed, and still serves api", async () => {
    const sent = mockUpstream();
    const web = await call("app.example.test/threats", { WEB_ORIGIN: "" });
    expect(web.status).toBe(503);
    expect(await web.text()).toBe("web is not deployed yet");
    const api = await call("app.example.test/api/v1/me", { WEB_ORIGIN: "" });
    expect(api.status).toBe(200);
    expect(sent.map((request) => request.url)).toEqual([`${API}/api/v1/me`]);
  });

  it.each([
    ["no API origin", "app.example.test/api/v1/me", { API_ORIGIN: "" }],
    ["no API origin, health", "app.example.test/api/healthz", { API_ORIGIN: "" }],
    ["no API origin, evidence", "evidence.example.test/e/tok3n", { API_ORIGIN: "" }],
    ["plain-HTTP API origin", "app.example.test/api/v1/me", { API_ORIGIN: "http://api.example" }],
    ["malformed API origin", "app.example.test/api/v1/me", { API_ORIGIN: "api-test.a.run.app" }],
    ["plain-HTTP web origin", "app.example.test/", { WEB_ORIGIN: "http://web.example" }],
    ["no secret, api", "app.example.test/api/v1/me", { EDGE_SHARED_SECRET: undefined }],
    ["empty secret, api", "app.example.test/api/v1/me", { EDGE_SHARED_SECRET: "" }],
    ["no secret, web", "app.example.test/", { EDGE_SHARED_SECRET: undefined }],
    ["no secret, health", "app.example.test/healthz", { EDGE_SHARED_SECRET: undefined }],
  ])("answers 503 and forwards nothing with %s", async (_, path, overrides: Partial<Env>) => {
    const sent = mockUpstream();
    const response = await call(path, overrides);
    expect(response.status).toBe(503);
    expect(await response.text()).toBe("edge is not configured");
    expect(sent).toHaveLength(0);
  });
});
