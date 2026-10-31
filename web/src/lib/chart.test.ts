import { describe, expect, it } from "vitest";

import { peak, xTicks, yMax } from "./chart";

const days = (values: number[]) =>
  values.map((detected, index) => ({
    day: `2026-09-${String(index + 1).padStart(2, "0")}`,
    detected,
    taken_down: 0,
  }));

describe("chart", () => {
  it("rounds the axis up to fives", () => {
    expect(yMax(days([0, 0]))).toBe(5);
    expect(yMax(days([3, 8, 2]))).toBe(10);
    expect(yMax(days([11]))).toBe(15);
  });

  it("labels five days of a month and every day of a week", () => {
    const month = days(Array.from({ length: 30 }, () => 1));
    expect(xTicks(month)).toEqual([
      "2026-09-01",
      "2026-09-08",
      "2026-09-15",
      "2026-09-22",
      "2026-09-30",
    ]);
    expect(xTicks(days([1, 2, 3, 4, 5, 6, 7]))).toHaveLength(7);
    expect(peak(days([1, 8, 3]))?.day).toBe("2026-09-02");
  });
});
