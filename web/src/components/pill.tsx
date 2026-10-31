import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export type PillTone = "neutral" | "waiting" | "success" | "danger" | "scheduled" | "brand";

const TONES: Record<PillTone, string> = {
  neutral: "border-control-border bg-segment text-text-strong",
  waiting:
    "border-[var(--status-notice-border)] bg-[var(--status-notice-bg)] text-[var(--status-notice-fg)]",
  success:
    "border-[var(--status-taken-border)] bg-[var(--status-taken-bg)] text-[var(--status-taken-fg)]",
  danger:
    "border-[var(--status-active-border)] bg-[var(--status-active-bg)] text-[var(--status-active-fg)]",
  scheduled: "border-dashed border-axis-line bg-panel text-text-secondary",
  brand: "border-transparent bg-brand-soft text-brand font-semibold tabular dark:text-link",
};

/** The small rounded labels of the threat detail (Your site, Clone, Received, Scheduled, ...). */
export function Pill({
  tone = "neutral",
  className,
  children,
}: {
  tone?: PillTone;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex flex-none items-center rounded-full border px-2 py-px text-xs leading-[18px] font-medium whitespace-nowrap",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
