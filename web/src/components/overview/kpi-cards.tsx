import { ArrowDown, ArrowUp } from "lucide-react";
import type { ReactNode } from "react";

import { Card, Hours, PanelHeader } from "@/components/states";
import { Meter } from "@/components/ui/meter";
import type { Schemas } from "@/lib/api/client";
import { count, plural } from "@/lib/format";
import { cn } from "@/lib/utils";

const ICON = "size-3.5 flex-none";

/**
 * A count against the previous period: an arrow and "19% more". More detections is not good or
 * bad news, so it stays neutral. Null when the previous period had nothing to compare with.
 */
export function ChangePill({ change }: { change: number | null }) {
  if (change === null) return null;
  if (change === 0) return <span className="font-semibold text-text">No change</span>;
  const Icon = change < 0 ? ArrowDown : ArrowUp;
  return (
    <span className="tabular inline-flex items-center gap-1 font-semibold text-text">
      <Icon aria-hidden="true" strokeWidth={1.5} className={ICON} />
      {Math.abs(change)}% {change < 0 ? "fewer" : "more"}
    </span>
  );
}

/** Median takedown time: only a shorter time turns green ("22% faster"). */
export function SpeedPill({ change }: { change: number | null }) {
  if (change === null) return null;
  if (change === 0) return <span className="font-semibold text-text">No change</span>;
  const faster = change < 0;
  const Icon = faster ? ArrowDown : ArrowUp;
  return (
    <span
      className={cn(
        "tabular inline-flex items-center gap-1 font-semibold",
        faster ? "text-st-good" : "text-text",
      )}
    >
      <Icon aria-hidden="true" strokeWidth={1.5} className={ICON} />
      {Math.abs(change)}% {faster ? "faster" : "slower"}
    </span>
  );
}

/** The line under a figure: the change and what it is measured against. */
function Delta({ change, children }: { change: number | null; children: ReactNode }) {
  if (change === null) return <>No earlier period to compare with</>;
  return (
    <>
      {children} {change === 0 ? "from previous period" : "than previous period"}
    </>
  );
}

const NUMBER =
  "font-display text-[34px] leading-[1.1] font-semibold tracking-[-0.01em] max-sm:text-[28px]";

function Figure({
  title,
  explain,
  value,
  footer,
  children,
}: {
  title: string;
  explain: string;
  value: ReactNode;
  footer: ReactNode;
  children?: ReactNode;
}) {
  return (
    // The negative margins tuck the first column's left border and the last row's bottom
    // border under the panel edge, so dividers only appear between figures.
    <article className="-mb-px -ml-px flex min-w-0 flex-col border-b border-l border-border px-5 pt-[18px] pb-5 max-sm:px-4 max-sm:pt-4 max-sm:pb-4">
      <h3>{title}</h3>
      <p className="mt-1 min-h-[2.9em] text-[13px] leading-[1.45] text-text-3">{explain}</p>
      <p className="mt-3 flex flex-wrap items-baseline gap-x-1.5">{value}</p>
      <p className="mt-1.5 text-[13px] leading-[1.45] text-text-2">{footer}</p>
      {children}
    </article>
  );
}

function Value({ value, unit }: { value: number; unit: string }) {
  return (
    <>
      <span className={NUMBER}>{count(value)}</span>
      <span className="text-[15px] font-medium text-text-2">{unit}</span>
    </>
  );
}

/** "This period at a glance": the four headline figures, each with its unit and comparison. */
export function KpiCards({
  kpis,
  changes,
  days,
}: {
  kpis: Schemas["KpisOut"];
  changes: Schemas["ChangesOut"];
  days: number;
}) {
  const rate = kpis.takedown_rate;
  return (
    <Card aria-labelledby="figures-title">
      <PanelHeader
        id="figures-title"
        title="This period at a glance"
        description={`Compared with the previous ${days} ${plural(days, "day", "days")}.`}
      />
      <div className="mt-4 grid grid-cols-4 overflow-hidden rounded-b-lg border-t border-border max-[1180px]:grid-cols-2">
        <Figure
          title="Detected"
          explain="Lookalike domains our scans found"
          value={<Value value={kpis.detected} unit={plural(kpis.detected, "domain", "domains")} />}
          footer={
            <Delta change={changes.detected}>
              <ChangePill change={changes.detected} />
            </Delta>
          }
        />
        <Figure
          title="Verified threats"
          explain="Confirmed as phishing or brand impersonation"
          value={<Value value={kpis.verified} unit={plural(kpis.verified, "threat", "threats")} />}
          footer={
            <Delta change={changes.verified}>
              <ChangePill change={changes.verified} />
            </Delta>
          }
        />
        <Figure
          title="Taken down"
          explain="Verified threats that stayed offline for 72 hours"
          value={<Value value={kpis.taken_down} unit={plural(kpis.taken_down, "site", "sites")} />}
          footer={
            rate === null ? (
              "No verified threats yet"
            ) : (
              <>
                <span className="tabular font-semibold text-text">{rate}%</span> of the{" "}
                {count(kpis.verified)} verified {plural(kpis.verified, "threat", "threats")}
              </>
            )
          }
        >
          {rate === null ? null : <Meter value={rate} className="mt-2.5 max-w-[220px]" />}
        </Figure>
        <Figure
          title="Median takedown time"
          explain="From our first abuse report to the first check that found the site offline"
          value={
            <Hours
              value={kpis.median_takedown_hours}
              tabular={false}
              className={NUMBER}
              unitClassName="ml-0.5 text-[22px] font-semibold tracking-normal text-text-2 max-sm:text-[18px]"
            />
          }
          footer={
            kpis.median_takedown_hours === null ? (
              "No takedowns in this period yet"
            ) : (
              <Delta change={changes.median_takedown_hours}>
                <SpeedPill change={changes.median_takedown_hours} />
              </Delta>
            )
          }
        />
      </div>
    </Card>
  );
}
