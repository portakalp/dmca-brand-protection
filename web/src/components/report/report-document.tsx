import { ArrowDown, ArrowUp, Shield } from "lucide-react";
import type { ReactNode } from "react";

import { ChannelsCard } from "@/components/overview/side-cards";
import { StatusCard } from "@/components/overview/status-card";
import { Card, Hours, PanelBody, PanelHeader } from "@/components/states";
import { Meter } from "@/components/ui/meter";
import type { ReportData } from "@/lib/api/report-data";
import { totals } from "@/lib/chart";
import { CHANNEL_LABELS, count, plural } from "@/lib/format";
import { longDay } from "@/lib/period";
import {
  METHOD_NOTES,
  previousMonthName,
  reportMoment,
  reportMonth,
  takedownTime,
} from "@/lib/report";
import { cn } from "@/lib/utils";

import { PrintChart } from "./print-chart";

/** The A4 content box at 96 dpi: 210 mm less the 12 mm side margins of the page rule. */
const CONTENT_WIDTH = 700;
/** The chart inside its panel: the content box less the panel's 20px padding and borders. */
const CHART_WIDTH = CONTENT_WIDTH - 42;

/**
 * A change against the month before, in words. More detections or verified threats is neither
 * good nor bad, so it stays neutral; only a faster median takedown turns green.
 */
function Delta({
  change,
  month,
  speed = false,
}: {
  change: number | null;
  month: string;
  /** The median takedown time: lower is faster, and faster is good. */
  speed?: boolean;
}) {
  if (change === null) return <span>No earlier month to compare with</span>;
  if (change === 0) return <span>Same as {month}</span>;
  const Icon = change < 0 ? ArrowDown : ArrowUp;
  const amount = Math.abs(change);
  const words = speed
    ? `${amount}% ${change < 0 ? "faster" : "slower"}`
    : `${amount}% ${change < 0 ? "fewer" : "more"}`;
  const good = speed && change < 0;
  return (
    <span className="inline-flex items-center gap-1">
      <Icon
        aria-hidden="true"
        strokeWidth={1.5}
        className={cn("size-3.5", good ? "text-st-good" : "text-text-2")}
      />
      <strong className={cn("font-semibold", good ? "text-st-good" : "text-text")}>{words}</strong>
      than {month}
    </span>
  );
}

function Figure({
  title,
  hint,
  value,
  unit,
  footer,
}: {
  title: string;
  hint: string;
  value: ReactNode;
  unit?: string;
  footer: ReactNode;
}) {
  return (
    <article className="flex min-w-0 flex-col gap-1 border-l border-border px-4 pt-3.5 pb-4 first:border-l-0">
      <h3 className="text-[12.5px]">{title}</h3>
      <p className="min-h-[30px] text-[10.5px] leading-[1.4] text-text-3">{hint}</p>
      <p className="mt-1 font-display text-[28px] leading-[1.1] font-semibold tracking-[-0.01em]">
        {value}
        {unit ? (
          <span className="ml-1.5 font-sans text-[12.5px] font-medium tracking-normal text-text-2">
            {unit}
          </span>
        ) : null}
      </p>
      <div className="mt-1 flex flex-col gap-1.5 text-[10.5px] text-text-2">{footer}</div>
    </article>
  );
}

function Figures({ data }: { data: ReportData }) {
  const { kpis, changes, previous_period } = data.overview;
  const month = previousMonthName(previous_period);
  return (
    <Card aria-labelledby="figures-title">
      <PanelHeader
        id="figures-title"
        title="This month at a glance"
        description={`Compared with ${month}.`}
        className="pb-3.5"
      />
      <div className="grid grid-cols-4 border-t border-border">
        <Figure
          title="Detected"
          hint="Lookalike domains our scans found"
          value={count(kpis.detected)}
          unit={plural(kpis.detected, "domain", "domains")}
          footer={<Delta change={changes.detected} month={month} />}
        />
        <Figure
          title="Verified threats"
          hint="Confirmed as phishing or brand impersonation"
          value={count(kpis.verified)}
          unit={plural(kpis.verified, "threat", "threats")}
          footer={<Delta change={changes.verified} month={month} />}
        />
        <Figure
          title="Taken down"
          hint="Verified threats that stayed offline for 72 hours"
          value={count(kpis.taken_down)}
          unit={plural(kpis.taken_down, "site", "sites")}
          footer={
            kpis.takedown_rate === null ? (
              <span>No verified threats</span>
            ) : (
              <>
                <span>
                  <strong className="font-semibold text-text">{kpis.takedown_rate}%</strong> of the{" "}
                  {count(kpis.verified)} verified {plural(kpis.verified, "threat", "threats")}
                </span>
                <Meter value={kpis.takedown_rate} />
              </>
            )
          }
        />
        <Figure
          title="Median takedown time"
          hint="From our first abuse notice to the site going offline"
          value={<Hours value={kpis.median_takedown_hours} />}
          footer={<Delta change={changes.median_takedown_hours} month={month} speed />}
        />
      </div>
    </Card>
  );
}

function Trend({ data }: { data: ReportData }) {
  const { daily } = data.overview;
  const sum = totals(daily);
  return (
    <Card aria-labelledby="trend-title">
      <PanelHeader
        id="trend-title"
        title="Detections and takedowns per day"
        description="Gray columns: lookalike domains we found. Blue line: sites that went offline."
      />
      <PanelBody className="flex flex-col gap-2 pt-3 pb-3">
        <p className="flex flex-wrap gap-x-5 gap-y-1 text-[11.5px] text-text-2">
          <span className="inline-flex items-center gap-2">
            <span aria-hidden="true" className="size-2.5 rounded-[2px] bg-context" />
            Detected{" "}
            <strong className="tabular font-semibold text-text">{count(sum.detected)}</strong>
          </span>
          <span className="inline-flex items-center gap-2">
            <span aria-hidden="true" className="h-0.5 w-4 rounded-[1px] bg-takedown" />
            Taken down{" "}
            <strong className="tabular font-semibold text-text">{count(sum.taken_down)}</strong>
          </span>
        </p>
        <PrintChart daily={daily} width={CHART_WIDTH} height={200} />
      </PanelBody>
    </Card>
  );
}

const TH =
  "border-y border-border bg-sunken px-2 py-1.5 text-left text-[11px] font-semibold whitespace-nowrap text-text-2 first:pl-3 last:pr-3";
const TD = "px-2 py-1.5 align-top first:pl-3 last:pr-3";

function TakenDown({ data }: { data: ReportData }) {
  const rows = data.taken_down;
  return (
    <section aria-labelledby="closed-title" className="flex flex-col gap-3">
      <div>
        <h2 id="closed-title">Threats taken down</h2>
        <p className="mt-[3px] text-[12px] text-text-3">
          {count(rows.length)} {plural(rows.length, "site", "sites")} first seen in{" "}
          {reportMonth(data.report.period)} and offline now. Time runs from our first notice to the
          site going offline; times are in {data.timezone}.
        </p>
      </div>
      {rows.length === 0 ? (
        <p className="text-[12.5px] text-text-3">No threat of this month is taken down yet.</p>
      ) : (
        <table className="w-full border-collapse text-[11.5px]">
          <thead>
            <tr>
              {["Case", "Domain", "First notice", "Offline since", "Time", "Channel"].map(
                (name) => (
                  <th key={name} scope="col" className={cn(TH, name === "Time" && "text-right")}>
                    {name}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.number} className="break-inside-avoid border-b border-border">
                <td className={cn(TD, "font-mono text-[11px] whitespace-nowrap text-text-2")}>
                  {row.number}
                </td>
                <td className={cn(TD, "font-mono font-medium break-all text-text")}>
                  {row.domain}
                </td>
                <td className={cn(TD, "tabular whitespace-nowrap text-text-2")}>
                  {reportMoment(row.first_notice_at, data.timezone)}
                </td>
                <td className={cn(TD, "tabular whitespace-nowrap text-text-2")}>
                  {reportMoment(row.taken_down_at, data.timezone)}
                </td>
                <td
                  className={cn(TD, "tabular text-right font-medium whitespace-nowrap text-text")}
                >
                  {takedownTime(row.takedown_hours)}
                </td>
                <td className={cn(TD, "whitespace-nowrap text-text-2")}>
                  {row.channel ? CHANNEL_LABELS[row.channel] : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Method() {
  return (
    <section aria-labelledby="method-title" className="flex break-inside-avoid flex-col gap-3">
      <div>
        <h2 id="method-title">How the numbers are counted</h2>
        <p className="mt-[3px] text-[12px] text-text-3">
          The same definitions as on your panel&apos;s overview for this month.
        </p>
      </div>
      <dl className="m-0 flex flex-col text-[11.5px] leading-[1.5]">
        {METHOD_NOTES.map((note) => (
          <div
            key={note.term}
            className="grid grid-cols-[150px_minmax(0,1fr)] gap-4 border-t border-border py-2 first:border-t-0 first:pt-0"
          >
            <dt className="font-semibold text-text">{note.term}</dt>
            <dd className="m-0 text-text-2">{note.text}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** The monthly report (§21): the panel's colours and type on A4. */
export function ReportDocument({ data }: { data: ReportData }) {
  const { period, live_threats: live, kpis } = data.overview;
  const month = reportMonth(data.report.period);
  return (
    <article
      className="report-document mx-auto flex flex-col gap-4 bg-surface text-text"
      style={{ width: CONTENT_WIDTH }}
    >
      <header className="flex items-end justify-between gap-4 border-b border-border pb-3.5">
        <div className="flex flex-col gap-1">
          <p className="flex items-center gap-2 text-[12px] font-medium text-text-2">
            <span
              aria-hidden="true"
              className="inline-flex size-[18px] items-center justify-center rounded-[4px] bg-accent text-on-accent"
            >
              <Shield className="size-3" strokeWidth={2} />
            </span>
            Kalkan brand protection report
          </p>
          <h1 className="text-[26px]">{month}</h1>
          <p className="text-[13px] text-text-2">{data.customer}</p>
        </div>
        <p className="text-right text-[11.5px] leading-[1.5] text-text-2">
          <span className="tabular">
            {longDay(period.start)} – {longDay(period.end)}
          </span>
          <br />
          {count(live)} {plural(live, "threat", "threats")} still live when printed
        </p>
      </header>
      <Figures data={data} />
      <Trend data={data} />
      <div className="grid break-before-page break-inside-avoid grid-cols-2 items-stretch gap-4 [&>section]:flex [&>section]:flex-col">
        <StatusCard
          distribution={data.overview.status_distribution}
          detected={data.overview.kpis.detected}
          compact
        />
        <ChannelsCard
          channels={data.overview.takedowns_by_channel}
          overall={kpis.median_takedown_hours}
        />
      </div>
      <div className="flex flex-col gap-6">
        <TakenDown data={data} />
        <Method />
      </div>
    </article>
  );
}
