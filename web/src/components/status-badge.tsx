import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import {
  INDICATOR_ICONS,
  STATUS_ICONS,
  STATUS_LABELS,
  STATUS_TONES,
  TONE_COLORS,
  indicatorColor,
  type IndicatorTone,
  type PanelStatus,
} from "@/lib/status";
import { cn } from "@/lib/utils";

/** A status icon in its reserved ink; decorative, so the word beside it carries the meaning. */
export function StatusIcon({ status, className }: { status: PanelStatus; className?: string }) {
  const Icon = STATUS_ICONS[status];
  return (
    <Icon
      aria-hidden="true"
      strokeWidth={1.5}
      className={cn("size-4 flex-none", className)}
      style={{ color: TONE_COLORS[STATUS_TONES[status]] }}
    />
  );
}

/**
 * A threat's status: icon + word ("Notice sent"). The icon carries the status ink; the word
 * stays in the text colour. No pill, no tinted background.
 */
export function Status({
  status,
  className,
  children,
}: {
  status: PanelStatus;
  className?: string;
  /** Extra text after the word, e.g. a count. Defaults to nothing. */
  children?: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-[7px] font-medium whitespace-nowrap text-text",
        className,
      )}
    >
      <StatusIcon status={status} />
      {STATUS_LABELS[status]}
      {children}
    </span>
  );
}

/** The old name of `Status`; same look. */
export const StatusBadge = Status;

/** Only the status icon, for lists that already show the word. Kept for older callers. */
export function StatusDot({ status }: { status: PanelStatus }) {
  return <StatusIcon status={status} />;
}

/**
 * A state that is not a threat status (a notice "Awaiting reply", "Acknowledged", "Scheduled",
 * "Still online", two-step verification "On"): icon + word in the same form as `Status`.
 */
export function Indicator({
  tone = "neutral",
  icon,
  className,
  children,
}: {
  tone?: IndicatorTone;
  /** Overrides the tone's default icon; pass null for a word with no icon. */
  icon?: LucideIcon | null;
  className?: string;
  children: ReactNode;
}) {
  const Icon = icon === undefined ? INDICATOR_ICONS[tone] : icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-[7px] font-medium whitespace-nowrap text-text",
        className,
      )}
    >
      {Icon ? (
        <Icon
          aria-hidden="true"
          strokeWidth={1.5}
          className="size-4 flex-none"
          style={{ color: indicatorColor(tone) }}
        />
      ) : null}
      {children}
    </span>
  );
}
