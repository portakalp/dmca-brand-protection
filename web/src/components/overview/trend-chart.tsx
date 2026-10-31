"use client";

import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";

import { Card, CardTitle } from "@/components/states";
import { ChartContainer, ChartTooltip, type ChartConfig } from "@/components/ui/chart";
import type { Schemas } from "@/lib/api/client";
import { peak, xTicks, yMax } from "@/lib/chart";
import { count } from "@/lib/format";
import { PRESETS, PRESET_DAYS, longDay, periodLabel, shortDay, type RangeKey } from "@/lib/period";
import { cn } from "@/lib/utils";

const CONFIG = {
  detected: { label: "Detected", color: "var(--chart-detected)" },
  taken_down: { label: "Taken down", color: "var(--chart-taken-down)" },
} satisfies ChartConfig;

type Payload = { payload: Schemas["DayOut"] };

function Tooltip({ active, payload }: { active?: boolean; payload?: readonly Payload[] }) {
  const day = active ? payload?.[0]?.payload : undefined;
  if (!day) return null;
  return (
    <div className="flex w-36 flex-col gap-1 rounded-control border border-control-border bg-popover px-2.5 py-2 text-[12px] leading-4 shadow-popover">
      <span className="font-semibold text-text">{longDay(day.day)}</span>
      {(["detected", "taken_down"] as const).map((key) => (
        <span key={key} className="flex items-center gap-1.5 text-text-secondary">
          <span className="size-2 rounded-[2px]" style={{ background: CONFIG[key].color }} />
          {CONFIG[key].label}
          <strong className="tabular ml-auto font-semibold text-text">{day[key]}</strong>
        </span>
      ))}
    </div>
  );
}

function Toggle({
  range,
  onChange,
}: {
  range: RangeKey;
  onChange: (key: (typeof PRESETS)[number]) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Chart period"
      className="inline-flex gap-0.5 rounded-control bg-segment p-[3px]"
    >
      {PRESETS.map((key) => (
        <button
          key={key}
          type="button"
          aria-pressed={range === key}
          onClick={() => onChange(key)}
          className={cn(
            "min-h-[34px] rounded-lg px-3 text-[13px] font-medium text-text-secondary",
            range === key && "bg-panel text-text shadow-segment",
          )}
        >
          {PRESET_DAYS[key]} days
        </button>
      ))}
    </div>
  );
}

export function TrendChart({
  daily,
  totals,
  period,
  range,
  onRange,
}: {
  daily: Schemas["DayOut"][];
  totals: { detected: number; taken_down: number };
  period: Schemas["PeriodOut"];
  range: RangeKey;
  onRange: (key: (typeof PRESETS)[number]) => void;
}) {
  const top = yMax(daily);
  const busiest = peak(daily);
  const description =
    `Daily detections and takedowns from ${longDay(period.start)} to ${longDay(period.end)}.` +
    (busiest && busiest.detected > 0
      ? ` Detections peaked at ${busiest.detected} on ${shortDay(busiest.day)}.`
      : "");
  return (
    <Card
      aria-labelledby="trend-title"
      className="flex min-w-0 flex-[2_1_540px] flex-col gap-4 px-5 pt-[18px] pb-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <CardTitle
          id="trend-title"
          title="Detections and takedowns"
          subtitle={`Per day, ${periodLabel(period.start, period.end).replace(/, \d{4}$/, "")}`}
        />
        <Toggle range={range} onChange={onRange} />
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-2 text-[13px] text-text-secondary">
        {(["detected", "taken_down"] as const).map((key) => (
          <span key={key} className="inline-flex items-center gap-2">
            <span
              aria-hidden="true"
              className="size-2.5 rounded-[3px]"
              style={{ background: CONFIG[key].color }}
            />
            {CONFIG[key].label}
            <strong className="tabular font-semibold text-text">{count(totals[key])}</strong>
          </span>
        ))}
      </div>
      <figure className="m-0">
        <ChartContainer
          config={CONFIG}
          className="aspect-auto h-[236px] w-full"
          role="img"
          aria-label={description}
        >
          <AreaChart data={daily} margin={{ top: 8, right: 18, bottom: 0, left: -18 }}>
            <CartesianGrid vertical={false} stroke="var(--grid-line)" strokeDasharray="3 4" />
            <YAxis
              domain={[0, top]}
              ticks={[0, top / 2, top]}
              tickLine={false}
              axisLine={false}
              allowDecimals={false}
              tick={{ fontSize: 11, fill: "var(--text-tertiary)" }}
              width={38}
            />
            <XAxis
              dataKey="day"
              ticks={xTicks(daily)}
              tickFormatter={shortDay}
              tickLine={false}
              axisLine={{ stroke: "var(--axis-line)" }}
              interval={0}
              tick={{ fontSize: 11, fill: "var(--text-tertiary)" }}
              tickMargin={6}
            />
            <ChartTooltip
              cursor={{ stroke: "var(--status-watching)", strokeDasharray: "3 3" }}
              content={<Tooltip />}
            />
            {(["detected", "taken_down"] as const).map((key) => (
              <Area
                key={key}
                dataKey={key}
                type="monotone"
                stroke={CONFIG[key].color}
                strokeWidth={2}
                fill={CONFIG[key].color}
                fillOpacity={key === "detected" ? 0.1 : 0.14}
                activeDot={{
                  r: 4,
                  fill: "var(--panel)",
                  stroke: CONFIG[key].color,
                  strokeWidth: 2,
                }}
                isAnimationActive={false}
              />
            ))}
          </AreaChart>
        </ChartContainer>
      </figure>
    </Card>
  );
}
