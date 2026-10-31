import type { Schemas } from "@/lib/api/client";

/** The print page's request header; the worker sends it, the API spends it (§17, §21). */
export const REPORT_TOKEN_HEADER = "x-report-token";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isReportId(value: string): boolean {
  return UUID.test(value);
}

const MONTH = new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric", timeZone: "UTC" });

/** "October 2026" for "2026-10". */
export function reportMonth(period: string): string {
  return MONTH.format(new Date(`${period}-01T00:00:00Z`));
}

/** "Oct 12, 14:05" in the customer's time zone. */
export function reportMoment(at: string | null, timeZone: string): string {
  if (!at) return "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(at));
}

/** "30.5 h" with one decimal: the table lists each takedown, the cards round to whole hours. */
export function takedownTime(hours: number | null): string {
  return hours === null ? "—" : `${hours.toFixed(1)} h`;
}

/** The months the report compares: "vs September". */
export function previousMonthName(period: Schemas["PeriodOut"]): string {
  return new Intl.DateTimeFormat("en-US", { month: "long", timeZone: "UTC" }).format(
    new Date(`${period.start}T00:00:00Z`),
  );
}

/** How the numbers are counted (product.md §12), printed at the end of every report. */
export const METHOD_NOTES: { term: string; text: string }[] = [
  {
    term: "Detected",
    text: "Lookalike sites first seen this month, sites you whitelisted excluded. Every other number counts these sites only, so the statuses add up to this number.",
  },
  {
    term: "Verified threats",
    text: "Detected sites we confirmed as phishing or impersonation, by now.",
  },
  {
    term: "Taken down",
    text: "Verified sites that are offline now and stayed offline for at least 72 hours.",
  },
  {
    term: "Takedown rate",
    text: "Taken down divided by verified threats.",
  },
  {
    term: "Median takedown time",
    text: "For the sites taken down: the middle value of the time from our first notice to the moment the site went offline.",
  },
  {
    term: "By channel",
    text: "Each takedown counts for the channel of the last notice sent before the site went offline; its time runs from that notice.",
  },
  {
    term: "Change",
    text: "The difference from the month before, in percent, with the same definitions.",
  },
];
