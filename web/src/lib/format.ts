import type { Schemas } from "@/lib/api/client";

import { todayIn } from "./period";

type Category = Schemas["Category"];
type Channel = Schemas["Channel"];
type WhitelistKind = Schemas["WhitelistKind"];

export const CATEGORY_LABELS: Record<Category, string> = {
  phishing_clone: "Phishing clone",
  brand_impersonation: "Brand impersonation",
  affiliate_or_review: "Affiliate or review site",
  parked_or_empty: "Parked lookalike",
  unrelated: "Unrelated site",
  unclear: "Unclear",
};

export function categoryLabel(category: Category | null): string {
  return category ? CATEGORY_LABELS[category] : "Under review";
}

export const CHANNEL_LABELS: Record<Channel, string> = {
  cloudflare: "Cloudflare",
  hosting: "Hosting provider",
  registrar: "Registrar",
  registry: "Registry",
  google: "Google",
};

export const WHITELIST_KIND_LABELS: Record<WhitelistKind, string> = {
  official: "Official site",
  mirror: "Alternate domain",
  affiliate: "Affiliate",
  partner: "Partner",
  social: "Social",
  other: "Other",
};

function parts(at: string, timeZone: string) {
  const date = new Date(at);
  const day = todayIn(timeZone, date);
  const time = new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date);
  const monthDay = new Intl.DateTimeFormat("en-US", {
    timeZone,
    month: "short",
    day: "numeric",
  }).format(date);
  return { day, time, monthDay };
}

/** "Today, 09:14" for today, "Oct 5" before (the design's First seen column). */
export function firstSeen(at: string, timeZone: string, now: Date = new Date()): string {
  const { day, time, monthDay } = parts(at, timeZone);
  return day === todayIn(timeZone, now) ? `Today, ${time}` : monthDay;
}

/** "10:05" for today, "Oct 7" before (after the last action's text). */
export function shortMoment(at: string, timeZone: string, now: Date = new Date()): string {
  const { day, time, monthDay } = parts(at, timeZone);
  return day === todayIn(timeZone, now) ? time : monthDay;
}

/** "Oct 5" in the customer's time zone. */
export function monthDay(at: string, timeZone: string): string {
  return parts(at, timeZone).monthDay;
}

export function lastAction(
  text: string | null,
  at: string | null,
  timeZone: string,
  now: Date = new Date(),
): string {
  if (!text) {
    return "—";
  }
  return at ? `${text} · ${shortMoment(at, timeZone, now)}` : text;
}

/** Whole hours for the panel ("31"), rounding half up like the API's percentages. */
export function hours(value: number | null): string {
  return value === null ? "—" : String(Math.floor(value + 0.5));
}

export function count(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}

/** "8 threats are still live. 6 are reported and waiting for takedown; 2 notices are …" */
export function liveSentence(notified: number, active: number): string {
  const parts: string[] = [];
  if (notified > 0) {
    parts.push(`${notified} ${plural(notified, "is", "are")} reported and waiting for takedown`);
  }
  if (active > 0) {
    parts.push(`${active} ${plural(active, "notice is", "notices are")} being prepared`);
  }
  return parts.length ? `${parts.join("; ")}.` : "";
}

/** Initials for the account button: "Mintera Security" → "MS", "jo@x.com" → "JO". */
export function initials(name: string | null, email: string): string {
  const source = name?.trim() || email.split("@")[0];
  const words = source.split(/[\s._-]+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[1][0] : source.slice(0, 2);
  return letters.toUpperCase();
}
