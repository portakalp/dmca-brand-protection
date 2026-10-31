"use client";

import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { Card, EmptyState, ErrorState, PanelHeader } from "@/components/states";
import { Skeleton } from "@/components/ui/skeleton";
import { useOverview } from "@/lib/api/queries";
import { daysBetween, parseRange, periodLabel, rangeDays, zoneName } from "@/lib/period";
import { sharedQuery, useUrlState } from "@/lib/url";

import { KpiCards } from "./kpi-cards";
import { LiveBanner } from "./live-banner";
import { RecentThreats } from "./recent-threats";
import { ChannelsCard, WhitelistCard } from "./side-cards";
import { StatusCard } from "./status-card";
import { TrendChart } from "./trend-chart";

const FALLBACK = "30d" as const;
/** The design shows six recent threats; the API sends a few more. */
const RECENT_ROWS = 6;
/** Two panels side by side: the wide one (chart, channels) and the narrow one. */
const ROW =
  "grid grid-cols-[minmax(0,1.75fr)_minmax(0,1fr)] items-stretch gap-5 max-[1180px]:grid-cols-1";

function OverviewSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading overview" className="flex flex-col gap-6">
      <Skeleton className="h-[104px] rounded-lg" />
      <Skeleton className="h-[240px] rounded-lg" />
      <div className={ROW}>
        <Skeleton className="h-[420px] rounded-lg" />
        <Skeleton className="h-[420px] rounded-lg" />
      </div>
      <div className={ROW}>
        <Skeleton className="h-[360px] rounded-lg" />
        <Skeleton className="h-[360px] rounded-lg" />
      </div>
    </div>
  );
}

export function OverviewView() {
  const { me, brand, timeZone } = usePanel();
  const { params } = useUrlState();
  const range = parseRange(params, FALLBACK);
  const days = rangeDays(range, timeZone);
  const overview = useOverview({
    brand: brand?.id ?? null,
    from: days?.from ?? null,
    to: days?.to ?? null,
  });
  const data = overview.data;
  const only = me.brands.length === 1 ? me.brands[0] : null;
  const domain = (brand ?? only)?.primary_domain;
  const subtitle = (
    <>
      Lookalike domains found for{" "}
      {domain ? <span className="font-mono text-[0.95em]">{domain}</span> : "all your brands"}
      {data ? `, ${periodLabel(data.period.start, data.period.end)}` : ""}. Times are in{" "}
      {zoneName(timeZone)}.
    </>
  );

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
          className="flex flex-col gap-6 transition-opacity aria-busy:opacity-60 motion-reduce:transition-none"
          aria-busy={overview.isPlaceholderData || undefined}
        >
          <LiveBanner live={data.live_threats} />
          <KpiCards
            kpis={data.kpis}
            changes={data.changes}
            days={daysBetween(data.previous_period.start, data.previous_period.end)}
          />
          <div className={ROW}>
            <TrendChart daily={data.daily} period={data.period} timeZone={timeZone} />
            <StatusCard distribution={data.status_distribution} detected={data.kpis.detected} />
          </div>
          <div className={ROW}>
            <ChannelsCard
              channels={data.takedowns_by_channel}
              overall={data.kpis.median_takedown_hours}
            />
            <WhitelistCard whitelist={data.whitelist} />
          </div>
          <Card id="recent" aria-labelledby="recent-title" className="overflow-hidden">
            <PanelHeader
              id="recent-title"
              title="Recent threats"
              description="Newest first. Open a row to see its evidence and takedown progress."
              actions={
                <Link
                  href={`/threats${sharedQuery(params)}`}
                  className="text-[13.5px] font-semibold text-accent underline underline-offset-[3px] hover:text-accent-hover"
                >
                  View all threats
                </Link>
              }
            />
            <div className="mt-3.5">
              {data.recent_threats.length ? (
                <RecentThreats threats={data.recent_threats.slice(0, RECENT_ROWS)} />
              ) : (
                <EmptyState title="No threats yet" className="border-t border-border">
                  We&apos;re watching new domains around the clock. Lookalikes of your brand show up
                  here as soon as we find them.
                </EmptyState>
              )}
            </div>
          </Card>
        </div>
      ) : null}
    </>
  );
}
