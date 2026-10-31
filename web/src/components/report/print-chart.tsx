"use client";

import { useEffect } from "react";
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";

import type { Schemas } from "@/lib/api/client";
import { xTicks, yMax } from "@/lib/chart";
import { shortDay } from "@/lib/period";

const SERIES = [
  { key: "detected", color: "var(--chart-detected)", opacity: 0.1 },
  { key: "taken_down", color: "var(--chart-taken-down)", opacity: 0.14 },
] as const;

/**
 * The overview's daily chart at a fixed paper size, without animation or tooltip. Once it is on
 * the page it marks the document ready: the printer waits for `html[data-report-ready]`.
 */
export function PrintChart({
  daily,
  width,
  height,
}: {
  daily: Schemas["DayOut"][];
  width: number;
  height: number;
}) {
  useEffect(() => {
    // Two frames: Recharts draws after its first commit.
    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() => {
        document.documentElement.dataset.reportReady = "1";
      });
    });
    return () => {
      cancelAnimationFrame(first);
      cancelAnimationFrame(second);
    };
  }, []);
  const top = yMax(daily);
  return (
    <AreaChart
      width={width}
      height={height}
      data={daily}
      margin={{ top: 8, right: 22, bottom: 0, left: -18 }}
    >
      <CartesianGrid vertical={false} stroke="var(--grid-line)" strokeDasharray="3 4" />
      <YAxis
        domain={[0, top]}
        ticks={[0, top / 2, top]}
        tickLine={false}
        axisLine={false}
        allowDecimals={false}
        tick={{ fontSize: 10, fill: "var(--text-tertiary)" }}
        width={38}
      />
      <XAxis
        dataKey="day"
        ticks={xTicks(daily)}
        tickFormatter={shortDay}
        tickLine={false}
        axisLine={{ stroke: "var(--axis-line)" }}
        interval={0}
        tick={{ fontSize: 10, fill: "var(--text-tertiary)" }}
        tickMargin={6}
      />
      {SERIES.map((series) => (
        <Area
          key={series.key}
          dataKey={series.key}
          type="monotone"
          stroke={series.color}
          strokeWidth={2}
          fill={series.color}
          fillOpacity={series.opacity}
          isAnimationActive={false}
          dot={false}
          activeDot={false}
        />
      ))}
    </AreaChart>
  );
}
