import { describe, expect, it } from "vitest";

import {
  addDays,
  daysBetween,
  parseRange,
  periodLabel,
  periodName,
  rangeDays,
  rangeLabel,
  rangeParams,
  todayIn,
  weekdayDay,
  zoneName,
} from "./period";

const params = (query: string) => new URLSearchParams(query);

describe("ranges", () => {
  it("counts days in the customer's time zone", () => {
    // 23:30 UTC on Oct 8 is already Oct 9 in Warsaw.
    const now = new Date("2026-10-08T23:30:00Z");
    expect(todayIn("Europe/Warsaw", now)).toBe("2026-10-09");
    expect(todayIn("UTC", now)).toBe("2026-10-08");
    expect(rangeDays({ key: "30d", from: null, to: null }, "UTC", now)).toEqual({
      from: "2026-09-09",
      to: "2026-10-08",
    });
    expect(rangeDays({ key: "7d", from: null, to: null }, "UTC", now)).toEqual({
      from: "2026-10-02",
      to: "2026-10-08",
    });
    expect(rangeDays({ key: "all", from: null, to: null }, "UTC", now)).toBeNull();
  });

  it("reads the URL and falls back on anything odd", () => {
    expect(parseRange(params(""), "30d").key).toBe("30d");
    expect(parseRange(params("range=90d"), "30d").key).toBe("90d");
    expect(parseRange(params("range=all"), "30d").key).toBe("all");
    expect(parseRange(params("range=12d"), "all").key).toBe("all");
    expect(parseRange(params("range=custom&from=2026-09-01&to=2026-09-10"), "30d")).toEqual({
      key: "custom",
      from: "2026-09-01",
      to: "2026-09-10",
    });
    // Reversed, malformed and longer than 366 days.
    expect(parseRange(params("range=custom&from=2026-09-10&to=2026-09-01"), "30d").key).toBe("30d");
    expect(parseRange(params("range=custom&from=2026-9-1&to=2026-09-10"), "30d").key).toBe("30d");
    expect(parseRange(params("range=custom&from=2025-01-01&to=2026-09-10"), "30d").key).toBe("30d");
  });

  it("keeps the default range out of the URL", () => {
    expect(rangeParams({ key: "30d", from: null, to: null }, "30d")).toEqual({
      range: null,
      from: null,
      to: null,
    });
    expect(rangeParams({ key: "7d", from: null, to: null }, "30d").range).toBe("7d");
    expect(rangeParams({ key: "custom", from: "2026-09-01", to: "2026-09-02" }, "30d")).toEqual({
      range: "custom",
      from: "2026-09-01",
      to: "2026-09-02",
    });
  });

  it("labels the design's way", () => {
    expect(periodLabel("2026-09-09", "2026-10-08")).toBe("Sep 9 – Oct 8, 2026");
    expect(periodLabel("2025-12-20", "2026-01-05")).toBe("Dec 20, 2025 – Jan 5, 2026");
    expect(rangeLabel({ key: "30d", from: null, to: null })).toBe("Last 30 days");
    expect(rangeLabel({ key: "custom", from: "2026-09-01", to: "2026-09-10" })).toBe(
      "Sep 1 – Sep 10",
    );
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    expect(daysBetween("2026-09-09", "2026-10-08")).toBe(30);
  });

  it("names the period, the tooltip day and the time zone", () => {
    const now = new Date("2026-10-31T12:00:00Z");
    expect(periodName("2026-10-02", "2026-10-31", "UTC", now)).toBe("Last 30 days");
    expect(periodName("2026-10-31", "2026-10-31", "UTC", now)).toBe("Today");
    expect(periodName("2026-09-01", "2026-09-10", "UTC", now)).toBe("Sep 1 – Sep 10");
    expect(weekdayDay("2026-10-17")).toBe("Sat, Oct 17");
    expect(zoneName("Europe/Warsaw", now)).toBe("CET");
    expect(zoneName("UTC", now)).toBe("UTC");
  });
});
