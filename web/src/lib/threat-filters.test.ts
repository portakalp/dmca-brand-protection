import { describe, expect, it } from "vitest";

import { pageCount, parseFilters, showing } from "./threat-filters";

describe("threat filters", () => {
  it("reads known values and ignores the rest", () => {
    expect(
      parseFilters(
        new URLSearchParams("status=notice_sent&category=phishing_clone&q=%20mintera%20"),
      ),
    ).toEqual({
      status: "notice_sent",
      category: "phishing_clone",
      q: "mintera",
      sort: "-first_seen_at",
      page: 1,
    });
    expect(
      parseFilters(new URLSearchParams("status=live&category=x&sort=evil&page=-3&q=")),
    ).toEqual({ status: null, category: null, q: null, sort: "-first_seen_at", page: 1 });
    expect(parseFilters(new URLSearchParams("sort=domain&page=4")).page).toBe(4);
  });

  it("pages by 25", () => {
    expect(pageCount(0)).toBe(1);
    expect(pageCount(128)).toBe(6);
    expect(showing(2, 128)).toBe("Showing 26–50 of 128");
    expect(showing(6, 128)).toBe("Showing 126–128 of 128");
    expect(showing(1, 0)).toBe("No threats");
  });
});
