import Link from "next/link";
import type { CSSProperties } from "react";

import { usePanel } from "@/components/shell/panel";
import { Card, Hours, PanelBody, PanelHeader } from "@/components/states";
import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import { CHANNEL_SCALE_HOURS, hourShare } from "@/lib/chart";
import { CHANNEL_LABELS, WHITELIST_KIND_LABELS, count, monthDay, plural } from "@/lib/format";
import { cn } from "@/lib/utils";

type Channel = Schemas["Channel"];

/** One plain line on who each abuse desk is. */
const CHANNEL_HINTS: Record<Channel, string> = {
  cloudflare: "Proxy and CDN",
  hosting: "Origin server host",
  registrar: "Domain seller",
  registry: "Top-level domain operator",
  google: "Safe Browsing",
};

const TICKS = [0, 24, 48, CHANNEL_SCALE_HOURS];

/**
 * Where an hour value sits across the track column. The rows and the overlays share the same
 * columns: `--lead` is the name column plus its gap, `--tail` the value column plus its gap; in
 * a narrow card both are 0 and the track runs the full width under the name.
 */
function at(hoursValue: number): CSSProperties {
  return {
    left: `calc(var(--lead) + (100% - var(--lead) - var(--tail)) * ${hourShare(hoursValue) / 100})`,
  };
}

const COLUMNS =
  "grid grid-cols-[140px_minmax(0,1fr)_110px] gap-x-4 @max-[480px]:grid-cols-[minmax(0,1fr)_auto] @max-[480px]:gap-x-3";

/**
 * "Takedowns by channel": the median hours from report to offline per abuse desk, on a 0–72 h
 * scale, with the all-channels median as one reference line across the rows.
 */
export function ChannelsCard({
  channels,
  overall = null,
  className,
}: {
  channels: Schemas["ChannelOut"][];
  /** The median over all channels (the Median takedown time figure); drawn as a reference. */
  overall?: number | null;
  className?: string;
}) {
  const rows = channels.filter((channel) => channel.cases > 0);
  return (
    <Card aria-labelledby="channels-title" className={cn("flex flex-col", className)}>
      <PanelHeader
        id="channels-title"
        title="Takedowns by channel"
        description="Where we sent the abuse report, and the median time until the site was found offline."
      />
      <PanelBody className="flex flex-1 flex-col">
        {rows.length === 0 ? (
          <p className="text-[13.5px] text-text-2">No takedowns in this period yet.</p>
        ) : (
          <div className="@container">
            <div className="[--lead:156px] [--tail:126px] @max-[480px]:[--lead:0px] @max-[480px]:[--tail:0px]">
              <div
                className={cn(
                  COLUMNS,
                  "border-b border-border pb-2 text-[12.5px] text-text-2 @max-[480px]:hidden",
                )}
                aria-hidden="true"
              >
                <span>Abuse desk</span>
                <span>Median hours from report to offline</span>
                <span className="text-right">Median · sites</span>
              </div>
              <div className="relative pt-7">
                {/* Gridlines and the all-channels reference, behind and across every row. */}
                {TICKS.slice(1).map((tick) => (
                  <span
                    key={tick}
                    aria-hidden="true"
                    className="absolute top-7 bottom-0 w-px bg-grid"
                    style={at(tick)}
                  />
                ))}
                {overall === null ? null : (
                  <>
                    <span
                      aria-hidden="true"
                      className="absolute top-1 bottom-0 z-10 w-px bg-ref"
                      style={at(overall)}
                    />
                    <span
                      className={cn(
                        "absolute top-1 z-10 text-[12px] font-semibold whitespace-nowrap text-text",
                        // Past the middle the label sits left of the line so it stays in the card.
                        hourShare(overall) > 55 ? "-translate-x-full pr-1.5" : "pl-1.5",
                      )}
                      style={at(overall)}
                    >
                      All channels <Hours value={overall} tabular={false} />
                    </span>
                  </>
                )}
                <ul className="relative">
                  {rows.map((channel) => (
                    <li
                      key={channel.channel}
                      className={cn(COLUMNS, "items-center py-2.5 @max-[480px]:gap-y-1.5")}
                    >
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate text-[13.5px] font-medium">
                          {CHANNEL_LABELS[channel.channel]}
                        </span>
                        <span className="truncate text-[12.5px] text-text-3 @max-[480px]:hidden">
                          {CHANNEL_HINTS[channel.channel]}
                        </span>
                      </span>
                      <span
                        aria-hidden="true"
                        className="block h-3.5 @max-[480px]:col-span-2 @max-[480px]:row-start-2"
                      >
                        {channel.median_hours === null ? null : (
                          <span
                            className="block h-full rounded-r-xs bg-takedown"
                            style={{ width: `${hourShare(channel.median_hours)}%` }}
                          />
                        )}
                      </span>
                      <span className="text-right text-[13px] whitespace-nowrap text-text-3">
                        <Hours value={channel.median_hours} className="font-semibold text-text" />
                        {" · "}
                        {count(channel.cases)} {plural(channel.cases, "site", "sites")}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
              <div aria-hidden="true" className="relative h-5 border-t border-axis">
                {TICKS.map((tick, index) => (
                  <span
                    key={tick}
                    className={cn(
                      "tabular absolute top-1 text-[12px] text-text-3",
                      index === 0
                        ? ""
                        : index === TICKS.length - 1
                          ? "-translate-x-full"
                          : "-translate-x-1/2",
                    )}
                    style={at(tick)}
                  >
                    {tick}
                  </span>
                ))}
              </div>
              <p className="mt-1 pr-[var(--tail)] pl-[var(--lead)] text-center text-[12px] text-text-2">
                Hours from report to offline
              </p>
            </div>
          </div>
        )}
        <p className="mt-auto pt-4 text-[12.5px] leading-[1.5] text-text-3">
          Measured to the first check that found the site offline. It counts as taken down once it
          stays offline for 72 hours, so the scale ends at 72 h.
        </p>
      </PanelBody>
    </Card>
  );
}

/** The design lists the four newest entries. */
const LATEST_ROWS = 4;

export function WhitelistCard({
  whitelist,
  className,
}: {
  whitelist: Schemas["WhitelistSummary"];
  className?: string;
}) {
  const { timeZone } = usePanel();
  const latest = whitelist.latest.slice(0, LATEST_ROWS);
  return (
    <Card aria-labelledby="whitelist-title" className={cn("flex flex-col", className)}>
      <PanelHeader
        id="whitelist-title"
        title="Whitelist"
        description="Your own domains and partners. We never report these."
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/whitelist" aria-label="Manage whitelist">
              Manage
            </Link>
          </Button>
        }
      />
      <PanelBody className="flex-1">
        <dl className="grid grid-cols-2 gap-4">
          {[
            {
              value: whitelist.total,
              label: plural(whitelist.total, "domain listed", "domains listed"),
            },
            { value: whitelist.added_this_month, label: "added this month" },
          ].map((item) => (
            <div key={item.label} className="flex flex-col-reverse">
              <dt className="text-[13px] text-text-2">{item.label}</dt>
              <dd className="font-display text-[26px] leading-[1.2] font-semibold tracking-[-0.01em]">
                {count(item.value)}
              </dd>
            </div>
          ))}
        </dl>
        {latest.length ? (
          <ul aria-label="Newest whitelist entries" className="mt-4 border-t border-border">
            {latest.map((entry) => (
              <li
                key={entry.id}
                className="flex items-baseline justify-between gap-x-3 border-b border-border py-2.5 last:border-b-0"
              >
                <span
                  className="min-w-0 truncate font-mono text-[12.5px] font-medium"
                  title={entry.pattern}
                >
                  {entry.pattern}
                </span>
                <span className="flex-none text-[12.5px] whitespace-nowrap text-text-3">
                  {WHITELIST_KIND_LABELS[entry.kind]} · {monthDay(entry.created_at, timeZone)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-4 border-t border-border pt-3 text-[13.5px] text-text-2">
            Nothing listed yet. Add your own domains so we never report them.
          </p>
        )}
      </PanelBody>
    </Card>
  );
}
