"use client";

import { useId, useState } from "react";
import { Bar, CartesianGrid, ComposedChart, Line, XAxis, YAxis } from "recharts";

import { Card, PanelBody, PanelHeader } from "@/components/states";
import { Button } from "@/components/ui/button";
import { ChartContainer, ChartTooltip, type ChartConfig } from "@/components/ui/chart";
import type { Schemas } from "@/lib/api/client";
import { peak, totals, xTicks, yMax, yTicks } from "@/lib/chart";
import { count } from "@/lib/format";
import { longDay, periodName, shortDay, weekdayDay } from "@/lib/period";
import { cn } from "@/lib/utils";

type Day = Schemas["DayOut"];

// Emphasis form: detections are the gray context, takedowns the one blue series.
const CONFIG = {
  detected: { label: "Detected", color: "var(--context)" },
  taken_down: { label: "Taken down", color: "var(--takedown)" },
} satisfies ChartConfig;

const TICK = { fill: "var(--text-3)", fontSize: 12 };
const AXIS_TITLE = { fill: "var(--text-2)", fontSize: 12 };

function DetectedKey({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn("size-2.5 flex-none rounded-[2px] bg-context", className)}
    />
  );
}

function TakenDownKey() {
  return <span aria-hidden="true" className="h-0.5 w-4 flex-none rounded-full bg-takedown" />;
}

function Tooltip({ active, payload }: { active?: boolean; payload?: readonly { payload: Day }[] }) {
  const day = active ? payload?.[0]?.payload : undefined;
  if (!day) return null;
  return (
    <div className="flex min-w-36 flex-col gap-1 rounded-sm bg-tooltip-bg px-2.5 py-2 text-[12.5px] leading-[1.35] text-tooltip-text">
      <span className="text-tooltip-muted">{weekdayDay(day.day)}</span>
      <span className="flex items-center gap-2">
        <DetectedKey />
        Detected
        <strong className="tabular ml-auto pl-3 font-semibold">{day.detected}</strong>
      </span>
      <span className="flex items-center gap-2">
        <TakenDownKey />
        Taken down
        <strong className="tabular ml-auto pl-3 font-semibold">{day.taken_down}</strong>
      </span>
    </div>
  );
}

function DayTable({ id, daily }: { id: string; daily: Day[] }) {
  const head =
    "border-y border-border bg-sunken px-3 py-[9px] text-[12.5px] font-semibold text-text-2";
  const cell = "border-b border-border px-3 py-2";
  return (
    <table id={id} className="w-full border-collapse text-[13.5px]">
      <caption className="sr-only">Detections and takedowns per day</caption>
      <thead>
        <tr>
          <th scope="col" className={cn(head, "text-left")}>
            Date
          </th>
          <th scope="col" className={cn(head, "text-right")}>
            Detected
          </th>
          <th scope="col" className={cn(head, "text-right")}>
            Taken down
          </th>
        </tr>
      </thead>
      <tbody>
        {daily.map((day) => (
          <tr key={day.day}>
            <th scope="row" className={cn(cell, "text-left font-normal text-text-2")}>
              {weekdayDay(day.day)}
            </th>
            <td className={cn(cell, "tabular text-right")}>{day.detected}</td>
            <td className={cn(cell, "tabular text-right")}>{day.taken_down}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * "Detections and takedowns per day": gray columns for the domains found each day and a blue
 * line for the sites that went offline. No area fill. Hover or arrow keys read a day; "Show as
 * table" gives the same rows as a table.
 */
export function TrendChart({
  daily,
  period,
  timeZone,
  className,
}: {
  daily: Day[];
  period: Schemas["PeriodOut"];
  timeZone: string;
  className?: string;
}) {
  const [asTable, setAsTable] = useState(false);
  const tableId = useId();
  const sums = totals(daily);
  const top = yMax(daily);
  const busiest = peak(daily);
  const sameYear = period.start.slice(0, 4) === period.end.slice(0, 4);
  const description =
    `Daily detections and takedowns from ${longDay(period.start)} to ${longDay(period.end)}.` +
    (busiest && busiest.detected > 0
      ? ` Detections peaked at ${busiest.detected} on ${shortDay(busiest.day)}.`
      : "");

  return (
    <Card aria-labelledby="trend-title" className={cn("flex flex-col", className)}>
      <PanelHeader
        id="trend-title"
        title="Detections and takedowns per day"
        description={`${periodName(period.start, period.end, timeZone)}. Gray columns: lookalike domains we found. Blue line: sites that went offline.`}
      />
      <PanelBody className="flex flex-1 flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-1.5">
          <div className="flex flex-wrap gap-x-5 gap-y-1.5 text-[13px] text-text-2">
            <span className="inline-flex items-center gap-2">
              <DetectedKey />
              Detected
              <strong className="tabular font-semibold text-text">{count(sums.detected)}</strong>
            </span>
            <span className="inline-flex items-center gap-2">
              <TakenDownKey />
              Taken down
              <strong className="tabular font-semibold text-text">{count(sums.taken_down)}</strong>
            </span>
          </div>
          <Button
            variant="link"
            size="sm"
            aria-expanded={asTable}
            aria-controls={asTable ? tableId : undefined}
            onClick={() => setAsTable((value) => !value)}
          >
            {asTable ? "Show as chart" : "Show as table"}
          </Button>
        </div>
        {asTable ? (
          <DayTable id={tableId} daily={daily} />
        ) : (
          <figure className="m-0 flex min-h-0 flex-1 flex-col rounded-sm outline-offset-2 has-[.recharts-surface:focus-visible]:outline-2 has-[.recharts-surface:focus-visible]:outline-focus">
            <figcaption className="sr-only">
              {description} Use the left and right arrow keys to read each day.
            </figcaption>
            <ChartContainer
              config={CONFIG}
              className="aspect-auto h-[300px] w-full max-sm:h-[240px] min-[1181px]:h-auto min-[1181px]:min-h-[400px] min-[1181px]:flex-1"
            >
              <ComposedChart
                accessibilityLayer
                data={daily}
                margin={{ top: 8, right: 8, bottom: 22, left: 4 }}
              >
                <CartesianGrid vertical={false} stroke="var(--grid)" />
                <XAxis
                  dataKey="day"
                  ticks={xTicks(daily)}
                  tickFormatter={shortDay}
                  interval={0}
                  tickLine={false}
                  axisLine={{ stroke: "var(--axis)" }}
                  tick={TICK}
                  tickMargin={6}
                  label={{
                    value: sameYear ? `Date, ${period.end.slice(0, 4)}` : "Date",
                    position: "insideBottom",
                    offset: -18,
                    ...AXIS_TITLE,
                  }}
                />
                <YAxis
                  domain={[0, top]}
                  ticks={yTicks(top)}
                  allowDecimals={false}
                  tickLine={false}
                  axisLine={false}
                  tick={TICK}
                  width={44}
                  label={{
                    value: "Domains per day",
                    angle: -90,
                    position: "insideLeft",
                    offset: 8,
                    style: { textAnchor: "middle" },
                    ...AXIS_TITLE,
                  }}
                />
                <ChartTooltip
                  cursor={{ stroke: "var(--text-3)", strokeWidth: 1 }}
                  content={<Tooltip />}
                  isAnimationActive={false}
                />
                <Bar
                  dataKey="detected"
                  fill="var(--context)"
                  maxBarSize={14}
                  radius={[1.5, 1.5, 0, 0]}
                  activeBar={{ fill: "var(--text-3)" }}
                  isAnimationActive={false}
                />
                {/* A surface halo first, so the line stays readable where it crosses columns. */}
                <Line
                  type="linear"
                  dataKey="taken_down"
                  stroke="var(--surface)"
                  strokeWidth={5}
                  dot={false}
                  activeDot={false}
                  legendType="none"
                  tooltipType="none"
                  isAnimationActive={false}
                />
                <Line
                  type="linear"
                  dataKey="taken_down"
                  stroke="var(--takedown)"
                  strokeWidth={2}
                  strokeLinejoin="round"
                  dot={false}
                  activeDot={{
                    r: 4.5,
                    stroke: "var(--surface)",
                    strokeWidth: 2,
                    fill: "var(--takedown)",
                  }}
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ChartContainer>
          </figure>
        )}
      </PanelBody>
    </Card>
  );
}
