import {
  CalendarClock,
  Check,
  CircleCheck,
  CircleMinus,
  Clock,
  Eye,
  OctagonAlert,
  Search,
  Send,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";

import type { Schemas } from "@/lib/api/client";

export type PanelStatus = Schemas["PanelStatus"];

/**
 * The reserved status scale. Each tone has one ink (globals.css `--st-*`), used only for the
 * icon and bar segment; the word next to it stays in the text colour.
 */
export type Tone = "critical" | "warning" | "good" | "neutral";

/** The panel's statuses in display order (product.md §12). */
export const STATUSES: PanelStatus[] = [
  "reviewing",
  "active",
  "notice_sent",
  "taken_down",
  "watching",
  "dismissed",
];

export const STATUS_LABELS: Record<PanelStatus, string> = {
  reviewing: "In review",
  active: "Active",
  notice_sent: "Notice sent",
  taken_down: "Taken down",
  watching: "Watching",
  dismissed: "Dismissed",
};

/** One plain line on what the status means for the customer. */
export const STATUS_HINTS: Record<PanelStatus, string> = {
  reviewing: "Being checked before any notice",
  active: "Live; we are preparing the notice",
  notice_sent: "Reported; waiting for the site to go offline",
  taken_down: "Offline for at least 72 hours",
  watching: "Parked lookalikes; rescanned every week",
  dismissed: "Affiliates, reviews and unrelated sites",
};

export const STATUS_TONES: Record<PanelStatus, Tone> = {
  reviewing: "neutral",
  active: "critical",
  notice_sent: "warning",
  taken_down: "good",
  watching: "neutral",
  dismissed: "neutral",
};

export const STATUS_ICONS: Record<PanelStatus, LucideIcon> = {
  reviewing: Search,
  active: OctagonAlert,
  notice_sent: Send,
  taken_down: CircleCheck,
  watching: Eye,
  dismissed: CircleMinus,
};

/** CSS colour of the icon (and of a bar segment) for each tone. */
export const TONE_COLORS: Record<Tone, string> = {
  critical: "var(--st-critical)",
  warning: "var(--st-warning)",
  good: "var(--st-good)",
  neutral: "var(--st-neutral)",
};

/**
 * The fill of each status in a stacked proportion bar. Dismissed takes the second neutral step
 * so it stays apart from Watching; the bar's aria-label still has to list every count.
 */
export const STATUS_BAR_COLORS: Record<PanelStatus, string> = {
  reviewing: "var(--st-neutral-2)",
  active: "var(--st-critical)",
  notice_sent: "var(--st-warning)",
  taken_down: "var(--st-good)",
  watching: "var(--st-neutral)",
  dismissed: "var(--st-neutral-2)",
};

/** Confirmed threats: live, reported, or already offline. */
export const VERIFIED_STATUSES: PanelStatus[] = ["active", "notice_sent", "taken_down"];
/** Not a threat now: parked lookalikes we keep watching, and dismissed sites. */
export const CLEARED_STATUSES: PanelStatus[] = ["watching", "dismissed"];

/** The status card lists In review only when something is being reviewed. */
export function visibleStatuses(distribution: Record<string, number>): PanelStatus[] {
  return STATUSES.filter((status) => status !== "reviewing" || (distribution[status] ?? 0) > 0);
}

/**
 * Default icons of the generic indicator tones (notices, checks, settings states). A caller may
 * pass its own icon; "scheduled" and "done" are neutral states with their own shapes.
 */
export type IndicatorTone = Tone | "scheduled" | "done";

export const INDICATOR_ICONS: Record<IndicatorTone, LucideIcon> = {
  critical: TriangleAlert,
  warning: Clock,
  good: CircleCheck,
  neutral: Eye,
  scheduled: CalendarClock,
  done: Check,
};

export function indicatorColor(tone: IndicatorTone): string {
  return tone === "scheduled" || tone === "done" ? TONE_COLORS.neutral : TONE_COLORS[tone];
}
