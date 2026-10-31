import { describe, expect, it } from "vitest";

import { safeNext, sharedQuery, withParams } from "./url";

describe("url state", () => {
  it("patches and sorts the query", () => {
    expect(withParams("page=2&status=active", { page: null, q: "mintera" })).toBe(
      "?q=mintera&status=active",
    );
    expect(withParams("", { status: "" })).toBe("");
    expect(withParams("b=1", { a: undefined })).toBe("?b=1");
  });

  it("carries only the brand and the range between pages", () => {
    expect(sharedQuery(new URLSearchParams("brand=x&range=7d&status=active&page=3"))).toBe(
      "?brand=x&range=7d",
    );
  });

  it("follows only same-site paths after sign-in", () => {
    expect(safeNext("/threats?status=active")).toBe("/threats?status=active");
    expect(safeNext("https://evil.example")).toBe("/overview");
    expect(safeNext("//evil.example")).toBe("/overview");
    expect(safeNext("/\\evil.example")).toBe("/overview");
    expect(safeNext("/login/mfa")).toBe("/overview");
    expect(safeNext("/reset-password?token=x")).toBe("/overview");
    expect(safeNext(null)).toBe("/overview");
  });
});
