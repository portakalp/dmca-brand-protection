"use client";

import { useEffect } from "react";
import { Bar, CartesianGrid, ComposedChart, Line, XAxis, YAxis } from "recharts";

import type { Schemas } from "@/lib/api/client";
import { xTicks, yMax, yTicks } from "@/lib/chart";
import { shortDay } from "@/lib/period";

const TICK = { fontSize: 10, fill: "var(--text-3)" };
const AXIS_TITLE = { fontSize: 10, fill: "var(--text-2)" };

/**
 * The overview's daily chart at a fixed paper size, without animation or tooltip: gray columns
 * for detections (context), a 2px blue line for takedowns on a surface halo. No fill under the
 * line. Once it is on the page it marks the document ready: the printer waits for
 * `html[data-report-ready]`.
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
    <ComposedChart
      width={width}
      height={height}
      data={daily}
      margin={{ top: 6, right: 8, bottom: 16, left: 6 }}
    >
      <CartesianGrid vertical={false} stroke="var(--grid)" />
      <YAxis
        domain={[0, top]}
        ticks={yTicks(top)}
        tickLine={false}
        axisLine={false}
        allowDecimals={false}
        tick={TICK}
        width={34}
        label={{
          value: "Domains per day",
          angle: -90,
          position: "insideLeft",
          offset: 4,
          style: { ...AXIS_TITLE, textAnchor: "middle" },
        }}
      />
      <XAxis
        dataKey="day"
        ticks={xTicks(daily)}
        tickFormatter={shortDay}
        tickLine={false}
        axisLine={{ stroke: "var(--axis)" }}
        interval={0}
        tick={TICK}
        tickMargin={6}
        label={{ value: "Date", position: "insideBottom", offset: -12, style: AXIS_TITLE }}
      />
      <Bar
        dataKey="detected"
        fill="var(--context)"
        maxBarSize={12}
        radius={[1.5, 1.5, 0, 0]}
        isAnimationActive={false}
      />
      <Line
        type="linear"
        dataKey="taken_down"
        stroke="var(--surface)"
        strokeWidth={5}
        dot={false}
        activeDot={false}
        isAnimationActive={false}
        legendType="none"
      />
      <Line
        type="linear"
        dataKey="taken_down"
        stroke="var(--takedown)"
        strokeWidth={2}
        strokeLinejoin="round"
        dot={false}
        activeDot={false}
        isAnimationActive={false}
      />
    </ComposedChart>
  );
}
