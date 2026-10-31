import type { Schemas } from "@/lib/api/client";

type Day = Schemas["DayOut"];

/** The y axis top: a multiple of 5 (10 for the design's month), at least 5. */
export function yMax(days: Day[]): number {
  const peak = Math.max(0, ...days.map((day) => Math.max(day.detected, day.taken_down)));
  return Math.max(5, Math.ceil(peak / 5) * 5);
}

/** Six evenly spaced whole-number ticks from 0 to the axis top (0, 2, … 10). */
export function yTicks(top: number): number[] {
  const step = top / 5;
  return Array.from({ length: 6 }, (_, index) => index * step);
}

/**
 * The days labelled under the chart: every day for a week, else one day a week from the first
 * (Oct 2, 9, 16, 23, 30), stepping in whole weeks so about five labels fit.
 */
export function xTicks(days: Day[]): string[] {
  if (days.length <= 8) return days.map((day) => day.day);
  const step = 7 * Math.max(1, Math.ceil(days.length / 5 / 7));
  return days.filter((_, index) => index % step === 0).map((day) => day.day);
}

/** The busiest day for the chart's text alternative. */
export function peak(days: Day[]): Day | null {
  return days.reduce<Day | null>(
    (best, day) => (best === null || day.detected > best.detected ? day : best),
    null,
  );
}

/** What the chart draws in total, for its legend. */
export function totals(days: Day[]): { detected: number; taken_down: number } {
  return days.reduce(
    (sum, day) => ({
      detected: sum.detected + day.detected,
      taken_down: sum.taken_down + day.taken_down,
    }),
    { detected: 0, taken_down: 0 },
  );
}

/** A whole percentage of a total, rounded half up like the API's; 0 when there is no total. */
export function percent(part: number, total: number): number {
  return total > 0 ? Math.floor((part / total) * 100 + 0.5) : 0;
}

/** The channel chart's scale: 0–72 h, the time after which a site counts as taken down. */
export const CHANNEL_SCALE_HOURS = 72;

/** Where an hour value sits on the channel scale, as a 0–100 share (past 72 h stays at the end). */
export function hourShare(value: number, scale: number = CHANNEL_SCALE_HOURS): number {
  return Math.max(0, Math.min(100, (value / scale) * 100));
}
