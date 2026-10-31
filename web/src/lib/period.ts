/**
 * The panel's date ranges: the last 7, 30 or 90 days, a custom range, or (on Threats) all time.
 * Days are calendar days in the customer's time zone, the same days the API counts (§17).
 */

export const PRESETS = ["7d", "30d", "90d"] as const;
export type Preset = (typeof PRESETS)[number];
export type RangeKey = Preset | "custom" | "all";

export const PRESET_DAYS: Record<Preset, number> = { "7d": 7, "30d": 30, "90d": 90 };
export const MAX_DAYS = 366;

export type Range = { key: RangeKey; from: string | null; to: string | null };

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Today in a time zone as YYYY-MM-DD. */
export function todayIn(timeZone: string, now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

export function addDays(day: string, days: number): string {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

export function daysBetween(from: string, to: string): number {
  const ms = Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`);
  return Math.round(ms / 86_400_000) + 1;
}

export function isIsoDate(value: string | null | undefined): value is string {
  return !!value && ISO_DATE.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
}

/** Reads the range from the URL; anything unknown falls back to the page's default. */
export function parseRange(params: URLSearchParams, fallback: RangeKey): Range {
  const key = params.get("range");
  if (key === "custom") {
    const from = params.get("from");
    const to = params.get("to");
    if (isIsoDate(from) && isIsoDate(to) && from <= to && daysBetween(from, to) <= MAX_DAYS) {
      return { key, from, to };
    }
    return { key: fallback, from: null, to: null };
  }
  if (key === "all" || (PRESETS as readonly string[]).includes(key ?? "")) {
    return { key: key as RangeKey, from: null, to: null };
  }
  return { key: fallback, from: null, to: null };
}

/** The first and last day the API is asked for; null for all time. */
export function rangeDays(range: Range, timeZone: string, now: Date = new Date()) {
  if (range.key === "all") {
    return null;
  }
  if (range.key === "custom") {
    return { from: range.from!, to: range.to! };
  }
  const to = todayIn(timeZone, now);
  return { from: addDays(to, 1 - PRESET_DAYS[range.key]), to };
}

/** The URL parameters of a range; the page's default range leaves the URL clean. */
export function rangeParams(range: Range, fallback: RangeKey): Record<string, string | null> {
  if (range.key === fallback && range.key !== "custom") {
    return { range: null, from: null, to: null };
  }
  if (range.key === "custom") {
    return { range: "custom", from: range.from, to: range.to };
  }
  return { range: range.key, from: null, to: null };
}

export function rangeLabel(range: Range): string {
  switch (range.key) {
    case "all":
      return "All time";
    case "custom":
      return `${shortDay(range.from!)} – ${shortDay(range.to!)}`;
    default:
      return `Last ${PRESET_DAYS[range.key]} days`;
  }
}

const MONTH_DAY = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});
const MONTH_DAY_YEAR = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

/** "Sep 9" for a YYYY-MM-DD day. */
export function shortDay(day: string): string {
  return MONTH_DAY.format(new Date(`${day}T00:00:00Z`));
}

/** "Sep 26, 2026" for a YYYY-MM-DD day. */
export function longDay(day: string): string {
  return MONTH_DAY_YEAR.format(new Date(`${day}T00:00:00Z`));
}

/** "Sep 9 – Oct 8, 2026" (the year once when both days share it). */
export function periodLabel(start: string, end: string): string {
  if (start.slice(0, 4) === end.slice(0, 4)) {
    return `${shortDay(start)} – ${longDay(end)}`;
  }
  return `${longDay(start)} – ${longDay(end)}`;
}
