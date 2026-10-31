import { describe, expect, it } from "vitest";

import { categoryLabel, firstSeen, hours, initials, lastAction, liveSentence } from "./format";

const NOW = new Date("2026-10-08T12:00:00Z");
const TZ = "Europe/Warsaw";

describe("format", () => {
  it("writes first seen as the design does", () => {
    expect(firstSeen("2026-10-08T07:14:00Z", TZ, NOW)).toBe("Today, 09:14");
    expect(firstSeen("2026-10-05T07:14:00Z", TZ, NOW)).toBe("Oct 5");
    // 22:30 UTC on Oct 7 is Oct 8 in Warsaw.
    expect(firstSeen("2026-10-07T22:30:00Z", TZ, NOW)).toBe("Today, 00:30");
  });

  it("joins the last action and its moment", () => {
    expect(lastAction("Host notified", "2026-10-08T08:05:00Z", TZ, NOW)).toBe(
      "Host notified · 10:05",
    );
    expect(lastAction("Sent to registrar", "2026-10-07T08:05:00Z", TZ, NOW)).toBe(
      "Sent to registrar · Oct 7",
    );
    expect(lastAction("Next rescan Oct 10", null, TZ, NOW)).toBe("Next rescan Oct 10");
    expect(lastAction(null, null, TZ, NOW)).toBe("—");
  });

  it("rounds hours half up and spells the live banner", () => {
    expect(hours(30.5)).toBe("31");
    expect(hours(30.49)).toBe("30");
    expect(hours(null)).toBe("—");
    expect(liveSentence(6, 2)).toBe(
      "6 are reported and waiting for takedown; 2 notices are being prepared.",
    );
    expect(liveSentence(1, 0)).toBe("1 is reported and waiting for takedown.");
    expect(liveSentence(0, 1)).toBe("1 notice is being prepared.");
    expect(liveSentence(0, 0)).toBe("");
  });

  it("labels categories and initials", () => {
    expect(categoryLabel("parked_or_empty")).toBe("Parked lookalike");
    expect(categoryLabel(null)).toBe("Under review");
    expect(initials("Mintera Security", "security@mintera.example")).toBe("MS");
    expect(initials(null, "jo.smith@example.com")).toBe("JS");
    expect(initials(null, "security@mintera.example")).toBe("SE");
  });
});
