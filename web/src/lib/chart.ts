import type { Schemas } from "@/lib/api/client";

type Day = Schemas["DayOut"];

/** The y axis top: a multiple of 5 (10 for the design's month), at least 5. */
export function yMax(days: Day[]): number {
  const peak = Math.max(0, ...days.map((day) => Math.max(day.detected, day.taken_down)));
  return Math.max(5, Math.ceil(peak / 5) * 5);
}

/** The days labelled under the chart: every day for a week, else five evenly spaced. */
export function xTicks(days: Day[]): string[] {
  if (days.length <= 8) return days.map((day) => day.day);
  const last = days.length - 1;
  const picks = [0, 0.24, 0.48, 0.72, 1].map((share) => Math.round(share * last));
  return [...new Set(picks)].map((index) => days[index].day);
}

/** The busiest day for the chart's text alternative. */
export function peak(days: Day[]): Day | null {
  return days.reduce<Day | null>(
    (best, day) => (best === null || day.detected > best.detected ? day : best),
    null,
  );
}
