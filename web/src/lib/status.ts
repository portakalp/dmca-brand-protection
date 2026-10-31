import type { Schemas } from "@/lib/api/client";

export type PanelStatus = Schemas["PanelStatus"];

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
  reviewing: "Reviewing",
  active: "Active",
  notice_sent: "Notice sent",
  taken_down: "Taken down",
  watching: "Watching",
  dismissed: "Dismissed",
};

export const STATUS_HINTS: Record<PanelStatus, string> = {
  reviewing: "Being checked before any notice",
  active: "Verified, notice being prepared",
  notice_sent: "Reported, waiting for takedown",
  taken_down: "Offline for at least 72 hours",
  watching: "Parked lookalikes, rescanned weekly",
  dismissed: "Affiliates, reviews and unrelated sites",
};

/** CSS variable stems of each status (globals.css). */
export const STATUS_TOKENS: Record<PanelStatus, string> = {
  reviewing: "reviewing",
  active: "active",
  notice_sent: "notice",
  taken_down: "taken",
  watching: "watching",
  dismissed: "dismissed",
};

/** The status card lists Reviewing only when something is being reviewed. */
export function visibleStatuses(distribution: Record<string, number>): PanelStatus[] {
  return STATUSES.filter((status) => status !== "reviewing" || (distribution[status] ?? 0) > 0);
}
