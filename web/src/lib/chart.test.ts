import { describe, expect, it } from "vitest";

import { hourShare, peak, percent, totals, xTicks, yMax, yTicks } from "./chart";

const days = (values: number[]) =>
  values.map((detected, index) => ({
    day: `2026-09-${String(index + 1).padStart(2, "0")}`,
    detected,
    taken_down: detected > 2 ? 1 : 0,
  }));

describe("chart", () => {
  it("rounds the axis up to fives and ticks it in fifths", () => {
    expect(yMax(days([0, 0]))).toBe(5);
    expect(yMax(days([3, 8, 2]))).toBe(10);
    expect(yMax(days([11]))).toBe(15);
    expect(yTicks(10)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(yTicks(5)).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it("labels a month once a week from its first day, and every day of a week", () => {
    const month = days(Array.from({ length: 30 }, () => 1));
    expect(xTicks(month)).toEqual([
      "2026-09-01",
      "2026-09-08",
      "2026-09-15",
      "2026-09-22",
      "2026-09-29",
    ]);
    expect(xTicks(days([1, 2, 3, 4, 5, 6, 7]))).toHaveLength(7);
    const quarter = Array.from({ length: 90 }, (_, index) => ({
      day: String(index),
      detected: 0,
      taken_down: 0,
    }));
    expect(xTicks(quarter)).toEqual(["0", "21", "42", "63", "84"]);
  });

  it("finds the busiest day and sums what the chart draws", () => {
    expect(peak(days([1, 8, 3]))?.day).toBe("2026-09-02");
    expect(peak([])).toBeNull();
    expect(totals(days([1, 8, 3]))).toEqual({ detected: 12, taken_down: 2 });
  });

  it("rounds shares half up and keeps hours on the 72 h scale", () => {
    expect(percent(39, 128)).toBe(30);
    expect(percent(69, 128)).toBe(54);
    expect(percent(2, 128)).toBe(2);
    expect(percent(1, 0)).toBe(0);
    expect(hourShare(36)).toBe(50);
    expect(hourShare(90)).toBe(100);
    expect(hourShare(-1)).toBe(0);
  });
});
