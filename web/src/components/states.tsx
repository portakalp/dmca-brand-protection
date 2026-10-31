import { Inbox, Info, OctagonAlert, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

/*
 * The panel's one container and its parts. A panel is surface + 1px border + 6px radius, no
 * shadow. Use it only where it groups something; inside a panel, group with dividers and gaps.
 *
 *   <Card aria-labelledby="x-title">
 *     <PanelHeader id="x-title" title="Threat status" description="Where each … stands today." />
 *     <PanelBody>…</PanelBody>
 *   </Card>
 */

export function Card({
  className,
  children,
  ...props
}: React.ComponentProps<"section"> & { children: ReactNode }) {
  return (
    <section
      className={cn("min-w-0 rounded-lg border border-border bg-surface", className)}
      {...props}
    >
      {children}
    </section>
  );
}

/** The same container under its design name. */
export const Panel = Card;

/**
 * A section heading: an h2 in the sans and, when the meaning is not obvious, one plain line on
 * what the section shows. `subtitle` is the old name of `description`.
 */
export function CardTitle({
  id,
  title,
  description,
  subtitle,
  className,
}: {
  id?: string;
  title: ReactNode;
  description?: ReactNode;
  subtitle?: ReactNode;
  className?: string;
}) {
  const desc = description ?? subtitle;
  return (
    <div className={cn("min-w-0", className)}>
      <h2 id={id}>{title}</h2>
      {desc ? <p className="mt-[3px] text-[13px] leading-[1.45] text-text-3">{desc}</p> : null}
    </div>
  );
}

/** The heading row of a panel: title and description on the left, actions on the right. */
export function PanelHeader({
  id,
  title,
  description,
  actions,
  className,
}: {
  id?: string;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-start justify-between gap-x-5 gap-y-3 px-5 pt-[18px] max-sm:px-4 max-sm:pt-4",
        className,
      )}
    >
      <CardTitle id={id} title={title} description={description} className="flex-[1_1_260px]" />
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/** The padded body under a PanelHeader. Tables that run edge to edge skip it. */
export function PanelBody({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn("px-5 pt-4 pb-5 max-sm:px-4 max-sm:pt-3.5 max-sm:pb-4", className)}>
      {children}
    </div>
  );
}

/**
 * A quiet explanatory box (sunken fill, border). For a warning or an error pass the tone; the
 * icon takes the status ink, the text stays readable.
 */
export function Note({
  tone = "neutral",
  icon,
  role,
  className,
  children,
}: {
  tone?: "neutral" | "warning" | "critical";
  icon?: LucideIcon;
  role?: "status" | "alert";
  className?: string;
  children: ReactNode;
}) {
  const Icon = icon ?? (tone === "neutral" ? Info : OctagonAlert);
  return (
    <div
      role={role}
      className={cn(
        "flex gap-2.5 rounded-sm border border-border bg-sunken px-3 py-2.5 text-[13px] leading-[1.45] text-text-2",
        className,
      )}
    >
      <Icon
        aria-hidden="true"
        strokeWidth={1.5}
        className={cn(
          "mt-px size-4 flex-none",
          tone === "neutral" && "text-text-3",
          tone === "warning" && "text-st-warning",
          tone === "critical" && "text-st-critical",
        )}
      />
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

/**
 * The one hour formatter: "31h", "4h 55m", "55m". Units are slightly smaller and spaced, but
 * the textContent stays "31h" / "4h 55m". `value` is in hours; null renders an em dash.
 */
export function Hours({
  value,
  minutes = false,
  tabular = true,
  className,
  unitClassName,
}: {
  value: number | null;
  /** Show minutes too ("4h 55m"); otherwise whole hours, rounded half up. */
  minutes?: boolean;
  /** Tabular digits line up in columns; a standalone figure reads better without them. */
  tabular?: boolean;
  className?: string;
  /** Extra classes for the unit letters, e.g. a smaller unit next to a large figure. */
  unitClassName?: string;
}) {
  if (value === null || !Number.isFinite(value)) {
    return <span className={className}>—</span>;
  }
  const unit = (u: string) => <span className={cn("ml-[0.12em]", unitClassName)}>{u}</span>;
  const digits = tabular ? "tabular" : "proportional-nums";
  if (!minutes) {
    return (
      <span className={cn(digits, "whitespace-nowrap", className)}>
        {Math.floor(value + 0.5)}
        {unit("h")}
      </span>
    );
  }
  const total = Math.max(0, Math.round(value * 60));
  const h = Math.floor(total / 60);
  const m = total % 60;
  return (
    <span className={cn(digits, "whitespace-nowrap", className)}>
      {h > 0 ? (
        <>
          {h}
          {unit("h")}
          {m > 0 ? " " : null}
        </>
      ) : null}
      {m > 0 || h === 0 ? (
        <>
          {m}
          {unit("m")}
        </>
      ) : null}
    </span>
  );
}

/** A failed request in words; the API's own text when it explains a refusal or a bad value. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403 && error.code === "read_only") return error.message;
    if (error.status === 403) return "You don't have access to this.";
    if (error.status === 404) return "We couldn't find this.";
    if (error.status >= 500) return "Something went wrong on our side. Please try again.";
    return error.message;
  }
  return "We couldn't load this. Check your connection and try again.";
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn("flex flex-wrap items-start gap-3 px-5 py-6 text-text-2", className)}
    >
      <OctagonAlert
        aria-hidden="true"
        strokeWidth={1.5}
        className="mt-px size-5 flex-none text-st-critical"
      />
      <div className="flex min-w-0 flex-[1_1_220px] flex-col items-start gap-3">
        <div>
          <p className="font-semibold text-text">This didn&apos;t load</p>
          <p className="text-[13.5px]">{errorMessage(error)}</p>
        </div>
        {onRetry ? (
          <Button variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  icon: Icon = Inbox,
  children,
  className,
}: {
  title: string;
  icon?: LucideIcon;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-start gap-3 px-5 py-6 text-text-2", className)}>
      <Icon aria-hidden="true" strokeWidth={1.5} className="mt-px size-5 flex-none text-text-3" />
      <div className="min-w-0">
        <p className="font-semibold text-text">{title}</p>
        {children ? <div className="mt-0.5 max-w-prose text-[13.5px]">{children}</div> : null}
      </div>
    </div>
  );
}

/** Placeholder lines while a panel loads; announced once as "Loading". */
export function LoadingState({
  rows = 3,
  label = "Loading",
  className,
}: {
  rows?: number;
  label?: string;
  className?: string;
}) {
  return (
    <div
      aria-busy="true"
      aria-label={label}
      role="status"
      className={cn("flex flex-col gap-3 px-5 py-5", className)}
    >
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className={cn("h-4", index === rows - 1 ? "w-2/3" : "w-full")} />
      ))}
    </div>
  );
}
