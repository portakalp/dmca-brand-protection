import { describe, expect, it } from "vitest";

import type { Schemas } from "./api/client";
import {
  fileNames,
  fileSize,
  matchedSignals,
  retainedUntil,
  fileLabel,
  shortHash,
  sortFiles,
} from "./evidence";

function file(id: string, kind: Schemas["ArtifactKind"], profile: Schemas["Profile"] | null) {
  return {
    id,
    kind,
    profile,
    bytes: 1,
    sha256: "x",
    content_type: "x",
    captured_at: `2026-10-08T09:1${id}:00Z`,
    retained_until: `2027-04-0${id}T00:00:00Z`,
    url: `/api/v1/artifacts/${id}`,
  } as Schemas["FileOut"];
}

describe("evidence", () => {
  it("writes sizes like the design", () => {
    expect([1_200_000, 640_000, 84_000, 2_300_000, 3_000, 512, 4_227_000].map(fileSize)).toEqual([
      "1.2 MB",
      "640 KB",
      "84 KB",
      "2.3 MB",
      "3 KB",
      "512 B",
      "4.2 MB",
    ]);
  });

  it("names and orders the package's files", () => {
    const files = sortFiles([
      file("1", "rdap", null),
      file("2", "har", "desktop"),
      file("3", "html", "desktop"),
      file("4", "screenshot", "mobile"),
      file("5", "screenshot", "desktop"),
      file("6", "screenshot", "desktop"),
    ]);
    const names = fileNames(files);
    expect(files.map((f) => names.get(f.id))).toEqual([
      "desktop.png",
      "desktop-2.png",
      "mobile.png",
      "page.html",
      "session.har",
      "rdap.json",
    ]);
    expect(retainedUntil(files)).toBe("2027-04-01T00:00:00Z");
  });

  it("says what each file is and shortens its hash", () => {
    expect(fileLabel(file("1", "screenshot", "desktop"))).toBe("Desktop screenshot");
    expect(fileLabel(file("2", "screenshot_full", "mobile"))).toBe("Mobile full-page screenshot");
    expect(fileLabel(file("3", "rdap", null))).toBe("Registration record");
    const hash = "3f9a62d1c07be44a19e8f3c2b5d6a7e80c4f1b29d73e6a5c8b0f4e2d10e7bc21e";
    expect(shortHash(hash)).toBe("3f9a62d1c07be44a19e8f3c2…0e7bc21e");
    expect(shortHash("abc")).toBe("abc");
  });

  it("lists the matched signals in the design's order", () => {
    expect(
      matchedSignals([
        { name: "brand_in_domain", value: 92 },
        { name: "favicon_match", value: true },
        { name: "logo_match", value: true },
        { name: "visual_similarity", value: 0.94 },
        { name: "form_card", value: true },
        { name: "external_form_action", value: false },
      ]),
    ).toEqual(["Logo match", "Same page layout", "Identical favicon", "Brand name in domain"]);
    expect(matchedSignals([{ name: "brand_in_domain", value: 40 }])).toEqual([]);
  });
});
