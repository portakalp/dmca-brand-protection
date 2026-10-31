import type { ReactNode } from "react";

import { ChangePill, SpeedPill } from "@/components/overview/kpi-cards";
import { ChannelsCard } from "@/components/overview/side-cards";
import { StatusCard } from "@/components/overview/status-card";
import { Card, CardTitle } from "@/components/states";
import type { ReportData } from "@/lib/api/report-data";
import { CHANNEL_LABELS, count, hours, plural } from "@/lib/format";
import { longDay } from "@/lib/period";
import {
  METHOD_NOTES,
  previousMonthName,
  reportMoment,
  reportMonth,
  takedownTime,
} from "@/lib/report";

import { PrintChart } from "./print-chart";

/** The A4 content box at 96 dpi: 210 mm less the 12 mm side margins of the page rule. */
const CONTENT_WIDTH = 700;
const CHART_WIDTH = CONTENT_WIDTH - 40;

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
    <article className="flex min-w-0 flex-col gap-1.5 rounded-card border border-card-border bg-card px-4 pt-3.5 pb-3">
      <h3 className="text-[12px] leading-4 font-medium text-text-secondary">{title}</h3>
      <p className="tabular text-[26px] leading-8 font-semibold tracking-[-0.025em]">
        {value}
        {unit ? (
          <span className="ml-[3px] text-[15px] font-medium tracking-normal text-text-secondary">
            {unit}
          </span>
        ) : null}
      </p>
      <p className="flex flex-wrap items-center gap-1 text-[11px] text-text-tertiary">{footer}</p>
    </article>
  );
}

function Kpis({ data }: { data: ReportData }) {
  const { kpis, changes, previous_period } = data.overview;
  const vs = `vs ${previousMonthName(previous_period)}`;
  const against = (change: number | null) => (change === null ? "No earlier data" : vs);
  return (
    <section aria-label="Key metrics" className="grid grid-cols-4 gap-3">
      <Kpi
        title="Detected"
        value={count(kpis.detected)}
        footer={
          <>
            <ChangePill change={changes.detected} />
            {against(changes.detected)}
          </>
        }
      />
      <Kpi
        title="Verified threats"
        value={count(kpis.verified)}
        footer={
          <>
            <ChangePill change={changes.verified} />
            {against(changes.verified)}
          </>
        }
      />
      <Kpi
        title="Taken down"
        value={count(kpis.taken_down)}
        footer={
          kpis.takedown_rate === null
            ? "No verified threats"
            : `${kpis.takedown_rate}% of verified threats`
        }
      />
      <Kpi
        title="Median takedown time"
        value={hours(kpis.median_takedown_hours)}
        unit={kpis.median_takedown_hours === null ? undefined : "h"}
        footer={
          <>
            <SpeedPill change={changes.median_takedown_hours} />
            {against(changes.median_takedown_hours)}
          </>
        }
      />
    </section>
  );
}

function Trend({ data }: { data: ReportData }) {
  const { daily, kpis } = data.overview;
  const takenDown = daily.reduce((sum, day) => sum + day.taken_down, 0);
  return (
    <Card aria-labelledby="trend-title" className="flex flex-col gap-3 px-5 pt-4 pb-3">
      <CardTitle id="trend-title" title="Detections and takedowns" subtitle="Per day" />
      <div className="flex gap-5 text-[12px] text-text-secondary">
        <span className="inline-flex items-center gap-2">
          <span aria-hidden="true" className="size-2.5 rounded-[3px] bg-[var(--chart-detected)]" />
          Detected{" "}
          <strong className="tabular font-semibold text-text">{count(kpis.detected)}</strong>
        </span>
        <span className="inline-flex items-center gap-2">
          <span
            aria-hidden="true"
            className="size-2.5 rounded-[3px] bg-[var(--chart-taken-down)]"
          />
          Taken down <strong className="tabular font-semibold text-text">{count(takenDown)}</strong>
        </span>
      </div>
      <PrintChart daily={daily} width={CHART_WIDTH} height={190} />
    </Card>
  );
}

function TakenDown({ data }: { data: ReportData }) {
  const rows = data.taken_down;
  return (
    <section aria-labelledby="closed-title" className="flex flex-col gap-3">
      <CardTitle
        id="closed-title"
        title="Threats taken down"
        subtitle={`${count(rows.length)} ${plural(rows.length, "site", "sites")} first seen in ${reportMonth(data.report.period)} and offline now; times in ${data.timezone}`}
      />
      {rows.length === 0 ? (
        <p className="text-[12.5px] text-text-tertiary">
          No threat of this month is taken down yet.
        </p>
      ) : (
        <table className="w-full border-collapse text-[11.5px]">
          <thead>
            <tr className="text-left text-[11px] text-text-secondary">
              {["Case", "Domain", "First notice", "Offline since", "Time", "Channel"].map(
                (name) => (
                  <th
                    key={name}
                    scope="col"
                    className="border-y border-card-border bg-table-head px-2 py-1.5 font-medium first:pl-3 last:pr-3"
                  >
                    {name}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.number} className="break-inside-avoid border-b border-row-divider">
                <td className="tabular px-2 py-1.5 pl-3 whitespace-nowrap text-text-secondary">
                  {row.number}
                </td>
                <td className="px-2 py-1.5 font-mono break-all text-text">{row.domain}</td>
                <td className="tabular px-2 py-1.5 whitespace-nowrap text-text-secondary">
                  {reportMoment(row.first_notice_at, data.timezone)}
                </td>
                <td className="tabular px-2 py-1.5 whitespace-nowrap text-text-secondary">
                  {reportMoment(row.taken_down_at, data.timezone)}
                </td>
                <td className="tabular px-2 py-1.5 whitespace-nowrap text-text">
                  {takedownTime(row.takedown_hours)}
                </td>
                <td className="px-2 py-1.5 pr-3 whitespace-nowrap text-text-secondary">
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
    <section aria-labelledby="method-title" className="flex break-inside-avoid flex-col gap-2.5">
      <CardTitle
        id="method-title"
        title="How the numbers are counted"
        subtitle="The same definitions as on your panel's overview for this month."
      />
      <dl className="grid grid-cols-[150px_1fr] gap-x-4 gap-y-1.5 text-[11.5px] leading-[17px]">
        {METHOD_NOTES.map((note) => (
          <div key={note.term} className="contents">
            <dt className="font-medium text-text">{note.term}</dt>
            <dd className="text-text-secondary">{note.text}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** The monthly report (§21): the panel's colours and type on A4. */
export function ReportDocument({ data }: { data: ReportData }) {
  const { period, live_threats: live } = data.overview;
  const month = reportMonth(data.report.period);
  return (
    <article
      className="report-document mx-auto flex flex-col gap-4 bg-panel text-text"
      style={{ width: CONTENT_WIDTH }}
    >
      <header className="flex items-end justify-between gap-4 border-b border-card-border pb-3">
        <div>
          <p className="text-[12px] font-medium tracking-[0.04em] text-text-tertiary uppercase">
            Kalkan · Brand protection report
          </p>
          <h1 className="text-[24px] leading-8 font-semibold tracking-[-0.02em]">{month}</h1>
          <p className="text-[13px] text-text-secondary">{data.customer}</p>
        </div>
        <p className="text-right text-[11.5px] leading-4 text-text-tertiary">
          {longDay(period.start)} – {longDay(period.end)}
          <br />
          {count(live)} {plural(live, "threat", "threats")} live when printed
        </p>
      </header>
      <Kpis data={data} />
      <Trend data={data} />
      <div className="grid grid-cols-2 gap-3 [&>section]:flex-auto">
        <StatusCard
          distribution={data.overview.status_distribution}
          detected={data.overview.kpis.detected}
        />
        <ChannelsCard channels={data.overview.takedowns_by_channel} />
      </div>
      <div className="flex break-before-page flex-col gap-6">
        <TakenDown data={data} />
        <Method />
      </div>
    </article>
  );
}
