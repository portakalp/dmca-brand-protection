import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import type { ReactNode } from "react";

import type { Schemas } from "@/lib/api/client";
import { count, hours } from "@/lib/format";
import { cn } from "@/lib/utils";

function Pill({ tone, children }: { tone: "neutral" | "good" | "bad"; children: ReactNode }) {
  return (
    <span
      className={cn(
        "tabular inline-flex items-center gap-[3px] rounded-full px-[7px] py-0.5 font-semibold",
        tone === "neutral" && "bg-segment text-text-strong",
        tone === "good" && "bg-success-bg text-success-fg",
        tone === "bad" && "bg-danger-bg text-danger-fg",
      )}
    >
      {children}
    </span>
  );
}

/** "↗ 19%" against the previous period; null when it had nothing to compare with. */
export function ChangePill({ change }: { change: number | null }) {
  if (change === null) return null;
  const Icon = change < 0 ? ArrowDownRight : ArrowUpRight;
  return (
    <Pill tone="neutral">
      <Icon aria-hidden="true" className="size-3" strokeWidth={2.5} />
      <span className="sr-only">{change < 0 ? "down" : "up"}</span>
      {Math.abs(change)}%
    </Pill>
  );
}

/** Median takedown time: a shorter time is better ("22% faster"). */
export function SpeedPill({ change }: { change: number | null }) {
  if (change === null) return null;
  if (change === 0) return <Pill tone="neutral">No change</Pill>;
  return change < 0 ? (
    <Pill tone="good">{Math.abs(change)}% faster</Pill>
  ) : (
    <Pill tone="bad">{change}% slower</Pill>
  );
}

function Kpi({
  title,
  value,
  unit,
  footer,
}: {
  title: string;
  value: string;
  unit?: string;
  footer: ReactNode;
}) {
  return (
    <article className="flex min-w-0 flex-[1_1_210px] flex-col gap-2 rounded-card border border-card-border bg-card px-[18px] pt-[18px] pb-4">
      <h2 className="text-[13px] leading-[18px] font-medium text-text-secondary">{title}</h2>
      <p className="tabular text-[32px] leading-[38px] font-semibold tracking-[-0.025em]">
        {value}
        {unit ? (
          <span className="ml-[3px] text-[18px] font-medium tracking-normal text-text-secondary">
            {unit}
          </span>
        ) : null}
      </p>
      <p className="flex flex-wrap items-center gap-1.5 text-[12.5px] text-text-tertiary">
        {footer}
      </p>
    </article>
  );
}

export function KpiCards({
  kpis,
  changes,
  days,
}: {
  kpis: Schemas["KpisOut"];
  changes: Schemas["ChangesOut"];
  days: number;
}) {
  const previous = `vs previous ${days} ${days === 1 ? "day" : "days"}`;
  const vs = (change: number | null) => (change === null ? "No earlier data" : previous);
  return (
    <section aria-label="Key metrics" className="flex flex-wrap gap-4">
      <Kpi
        title="Detected"
        value={count(kpis.detected)}
        footer={
          <>
            <ChangePill change={changes.detected} />
            {vs(changes.detected)}
          </>
        }
      />
      <Kpi
        title="Verified threats"
        value={count(kpis.verified)}
        footer={
          <>
            <ChangePill change={changes.verified} />
            {vs(changes.verified)}
          </>
        }
      />
      <Kpi
        title="Taken down"
        value={count(kpis.taken_down)}
        footer={
          kpis.takedown_rate === null ? (
            "No verified threats yet"
          ) : (
            <>
              <Pill tone="good">{kpis.takedown_rate}%</Pill>
              of verified threats
            </>
          )
        }
      />
      <Kpi
        title="Median takedown time"
        value={hours(kpis.median_takedown_hours)}
        unit={kpis.median_takedown_hours === null ? undefined : "h"}
        footer={
          <>
            <SpeedPill change={changes.median_takedown_hours} />
            {vs(changes.median_takedown_hours)}
          </>
        }
      />
    </section>
  );
}
