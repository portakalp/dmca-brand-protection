"use client";

import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { brandLabel, usePanel } from "@/components/shell/panel";
import { Card, CardTitle, EmptyState, ErrorState } from "@/components/states";
import { ThreatTable } from "@/components/threats/threat-table";
import { Skeleton } from "@/components/ui/skeleton";
import { useOverview } from "@/lib/api/queries";
import { daysBetween, parseRange, periodLabel, rangeDays, rangeParams } from "@/lib/period";
import { sharedQuery, useUrlState } from "@/lib/url";

import { KpiCards } from "./kpi-cards";
import { LiveBanner } from "./live-banner";
import { ChannelsCard, WhitelistCard } from "./side-cards";
import { StatusCard } from "./status-card";
import { TrendChart } from "./trend-chart";

const FALLBACK = "30d" as const;
/** The design shows six recent threats; the API sends a few more. */
const RECENT_ROWS = 6;

function OverviewSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading overview" className="flex flex-col gap-5">
      <Skeleton className="h-14 rounded-xl" />
      <div className="flex flex-wrap gap-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-[132px] flex-[1_1_210px] rounded-card" />
        ))}
      </div>
      <div className="flex flex-wrap gap-4">
        <Skeleton className="h-[388px] flex-[2_1_540px] rounded-card" />
        <Skeleton className="h-[388px] flex-[1_1_280px] rounded-card" />
      </div>
      <div className="flex flex-wrap gap-4">
        <Skeleton className="h-[420px] flex-[2_1_540px] rounded-card" />
        <Skeleton className="h-[420px] flex-[1_1_280px] rounded-card" />
      </div>
    </div>
  );
}

export function OverviewView() {
  const { me, brand, timeZone } = usePanel();
  const { params, update } = useUrlState();
  const range = parseRange(params, FALLBACK);
  const days = rangeDays(range, timeZone);
  const overview = useOverview({
    brand: brand?.id ?? null,
    from: days?.from ?? null,
    to: days?.to ?? null,
  });
  const data = overview.data;
  const subtitle = data
    ? `${brandLabel(me, brand)} · ${periodLabel(data.period.start, data.period.end)}`
    : brandLabel(me, brand);

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle={subtitle}
        range={{ value: range, fallback: FALLBACK }}
        exportReport
      />
      {overview.isPending ? (
        <OverviewSkeleton />
      ) : overview.isError && !data ? (
        <Card>
          <ErrorState error={overview.error} onRetry={() => overview.refetch()} />
        </Card>
      ) : data ? (
        <div
          className="flex flex-col gap-5 transition-opacity aria-busy:opacity-60"
          aria-busy={overview.isPlaceholderData || undefined}
        >
          <LiveBanner live={data.live_threats} />
          <KpiCards
            kpis={data.kpis}
            changes={data.changes}
            days={daysBetween(data.period.start, data.period.end)}
          />
          <div className="flex flex-wrap items-stretch gap-4">
            <TrendChart
              daily={data.daily}
              totals={{ detected: data.kpis.detected, taken_down: data.kpis.taken_down }}
              period={data.period}
              range={range.key}
              onRange={(key) => update(rangeParams({ key, from: null, to: null }, FALLBACK))}
            />
            <StatusCard distribution={data.status_distribution} detected={data.kpis.detected} />
          </div>
          <div className="flex flex-wrap items-stretch gap-4">
            <Card
              id="recent"
              aria-labelledby="recent-title"
              className="flex flex-[2_1_540px] flex-col overflow-hidden"
            >
              <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                <CardTitle id="recent-title" title="Recent threats" subtitle="Newest first" />
                <Link
                  href={`/threats${sharedQuery(params)}`}
                  className="inline-flex min-h-10 items-center text-[13.5px] font-medium"
                >
                  View all threats
                </Link>
              </div>
              {data.recent_threats.length ? (
                <ThreatTable
                  threats={data.recent_threats.slice(0, RECENT_ROWS)}
                  caption="Recent threats, newest first"
                />
              ) : (
                <EmptyState title="No threats yet" className="border-t border-card-border">
                  We&apos;re watching new domains around the clock. Lookalikes of your brand show up
                  here as soon as we find them.
                </EmptyState>
              )}
            </Card>
            <div className="flex min-w-0 flex-[1_1_280px] flex-col gap-4">
              <ChannelsCard channels={data.takedowns_by_channel} />
              <WhitelistCard whitelist={data.whitelist} />
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
