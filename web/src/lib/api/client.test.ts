import { describe, expect, it } from "vitest";

import { ApiError, csrf, signInTarget, unwrap } from "./client";

type OnRequest = (options: { request: Request }) => Request;

describe("api client", () => {
  it("adds the CSRF header to state changes only", async () => {
    const onRequest = csrf.onRequest as unknown as OnRequest;
    const post = onRequest({
      request: new Request("http://x/api/v1/whitelist", { method: "POST" }),
    });
    const get = onRequest({ request: new Request("http://x/api/v1/me") });
    expect(post.headers.get("X-Kalkan-CSRF")).toBe("1");
    expect(get.headers.has("X-Kalkan-CSRF")).toBe(false);
  });

  it("sends a missing second factor to /login/mfa", () => {
    expect(signInTarget("mfa_required", "/threats?q=a")).toBe("/login/mfa?next=%2Fthreats%3Fq%3Da");
    expect(signInTarget(null, "/overview")).toBe("/login?next=%2Foverview");
  });

  it("turns a problem into an ApiError", () => {
    const response = new Response(null, { status: 429 });
    try {
      unwrap({ error: { title: "Too many", code: "locked", retry_after: 900 }, response });
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).code).toBe("locked");
      expect((error as ApiError).message).toBe("Too many");
      expect((error as ApiError).body.retry_after).toBe(900);
    }
    expect(unwrap({ data: { ok: 1 }, response: new Response("{}") })).toEqual({ ok: 1 });
  });
});
