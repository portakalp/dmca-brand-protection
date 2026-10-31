"use client";

import { ArrowRight, CircleCheck, OctagonAlert } from "lucide-react";
import Link from "next/link";

import { usePanel } from "@/components/shell/panel";
import { Card } from "@/components/states";
import { StatusIcon } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useThreatCount } from "@/lib/api/queries";
import { count, plural } from "@/lib/format";
import { STATUS_BAR_COLORS, STATUS_LABELS, type PanelStatus } from "@/lib/status";
import { withParams } from "@/lib/url";

/** The two live statuses, in the order the strip shows them, with what each means. */
const LIVE: { status: PanelStatus; meaning: string }[] = [
  { status: "active", meaning: "we are preparing the notice" },
  { status: "notice_sent", meaning: "reported, waiting for the site to go offline" },
];

/**
 * The live-threats strip: how many sites are still reachable, split into Active and Notice sent
 * with direct labels over a proportion bar, and a way to review them.
 */
export function LiveBanner({ live }: { live: number }) {
  const { brand } = usePanel();
  const brandId = brand?.id ?? null;
  const notified = useThreatCount(brandId, "notice_sent", live > 0);
  const active = useThreatCount(brandId, "active", live > 0);

  if (live === 0) {
    return (
      <Card aria-labelledby="live-title" className="flex items-start gap-3.5 px-5 py-4 max-sm:px-4">
        <CircleCheck
          aria-hidden="true"
          strokeWidth={1.5}
          className="mt-0.5 size-5 flex-none text-st-good"
        />
        <div className="min-w-0">
          <h2 id="live-title" className="font-sans text-[15px] font-semibold">
            No threats are live.
          </h2>
          <p className="text-[13.5px] text-text-2">
            Everything we found is taken down, watched or dismissed.
          </p>
        </div>
      </Card>
    );
  }

  const counts: Record<string, number | undefined> = {
    active: active.data,
    notice_sent: notified.data,
  };
  const loaded = active.data !== undefined && notified.data !== undefined;
  const parts = LIVE.filter(({ status }) => (counts[status] ?? 0) > 0);
  const status = (notified.data ?? 0) >= (active.data ?? 0) ? "notice_sent" : "active";
  const href = `/threats${withParams("", { brand: brandId, status })}`;

  return (
    <Card
      aria-labelledby="live-title"
      className="grid grid-cols-[minmax(0,320px)_minmax(0,1fr)_auto] items-center gap-x-8 gap-y-4 px-5 py-[18px] max-lg:grid-cols-[minmax(0,1fr)_auto] max-sm:grid-cols-1 max-sm:px-4 max-sm:py-4"
    >
      <div className="flex items-start gap-3.5">
        <span
          aria-hidden="true"
          className="inline-flex size-9 flex-none items-center justify-center rounded-full border border-st-critical text-st-critical"
        >
          <OctagonAlert strokeWidth={1.5} className="size-[18px]" />
        </span>
        <div className="min-w-0">
          <h2 id="live-title" className="font-sans text-[15px] font-semibold">
            {count(live)} {plural(live, "threat is", "threats are")} still live.
          </h2>
          <p className="text-[13px] leading-[1.45] text-text-2">
            {plural(live, "This site", "These sites")} can still be reached by your customers.
          </p>
        </div>
      </div>
      <div className="min-w-0 max-lg:order-last max-lg:col-span-full max-sm:order-none">
        {loaded && parts.length ? (
          <>
            <div className="flex gap-0.5">
              {parts.map(({ status: key }) => (
                <div
                  key={key}
                  className="flex min-w-fit flex-col gap-1.5"
                  style={{ flex: `${counts[key]} 1 0` }}
                >
                  <span className="inline-flex items-center gap-1.5 pr-2 text-[13px] whitespace-nowrap text-text">
                    <StatusIcon status={key} className="size-3.5" />
                    <span className="tabular font-semibold">{count(counts[key] ?? 0)}</span>
                    {STATUS_LABELS[key]}
                  </span>
                  <span
                    aria-hidden="true"
                    className="block h-2.5 rounded-xs"
                    style={{ background: STATUS_BAR_COLORS[key] }}
                  />
                </div>
              ))}
            </div>
            <p className="mt-2 text-[12.5px] leading-[1.45] text-text-3">
              {parts
                .map(({ status: key, meaning }) => `${STATUS_LABELS[key]}: ${meaning}.`)
                .join(" ")}
            </p>
          </>
        ) : loaded ? null : (
          <div role="status" aria-label="Loading live threats" className="flex flex-col gap-2">
            <Skeleton className="h-3.5 w-40" />
            <Skeleton className="h-2.5 w-full" />
          </div>
        )}
      </div>
      <Button asChild className="max-sm:w-full">
        <Link href={href}>
          Review live threats
          <ArrowRight aria-hidden="true" />
        </Link>
      </Button>
    </Card>
  );
}
