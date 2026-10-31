import type { ReactNode } from "react";

import type { IndicatorTone } from "@/lib/status";
import { cn } from "@/lib/utils";

import { Indicator } from "./status-badge";

export type PillTone = "neutral" | "waiting" | "success" | "danger" | "scheduled" | "brand";

const TONES: Record<PillTone, IndicatorTone | null> = {
  neutral: null,
  waiting: "warning",
  success: "good",
  danger: "critical",
  scheduled: "scheduled",
  brand: null,
};

/**
 * The old pill API, now drawn as the status indicator (icon + word, no tinted pill). "neutral"
 * and "brand" are plain words with no icon. New code uses `Indicator` from status-badge.tsx.
 */
export function Pill({
  tone = "neutral",
  className,
  children,
}: {
  tone?: PillTone;
  className?: string;
  children: ReactNode;
}) {
  const indicator = TONES[tone];
  return (
    <Indicator
      tone={indicator ?? "neutral"}
      icon={indicator ? undefined : null}
      className={cn(tone === "neutral" && "text-text-2", className)}
    >
      {children}
    </Indicator>
  );
}
