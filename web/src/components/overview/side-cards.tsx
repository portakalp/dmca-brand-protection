import Link from "next/link";

import { usePanel } from "@/components/shell/panel";
import { Card, CardTitle } from "@/components/states";
import type { Schemas } from "@/lib/api/client";
import { CHANNEL_LABELS, WHITELIST_KIND_LABELS, count, hours, monthDay } from "@/lib/format";

export function ChannelsCard({ channels }: { channels: Schemas["ChannelOut"][] }) {
  const top = Math.max(1, ...channels.map((channel) => channel.cases));
  const rows = channels.filter((channel) => channel.cases > 0);
  return (
    <Card aria-labelledby="channels-title" className="flex flex-col gap-3.5 px-5 py-[18px]">
      <CardTitle
        id="channels-title"
        title="Takedowns by channel"
        subtitle="Count and median time to takedown"
      />
      {rows.length === 0 ? (
        <p className="text-[13px] text-text-tertiary">No takedowns in this period yet.</p>
      ) : (
        <ul className="flex flex-col gap-3.5">
          {rows.map((channel) => (
            <li key={channel.channel} className="flex flex-col gap-[7px]">
              <span className="flex justify-between gap-3 text-[13.5px]">
                <span className="font-medium">{CHANNEL_LABELS[channel.channel]}</span>
                <span className="tabular text-text-secondary">
                  {count(channel.cases)}
                  {channel.median_hours === null ? "" : ` · ${hours(channel.median_hours)} h`}
                </span>
              </span>
              <span aria-hidden="true" className="block h-1.5 rounded-full bg-segment">
                <span
                  className="block h-1.5 rounded-full bg-brand"
                  style={{ width: `${Math.round((channel.cases / top) * 100)}%` }}
                />
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/** The design lists the three newest entries. */
const LATEST_ROWS = 3;

export function WhitelistCard({ whitelist }: { whitelist: Schemas["WhitelistSummary"] }) {
  const { timeZone } = usePanel();
  return (
    <Card
      aria-labelledby="whitelist-title"
      className="flex flex-auto flex-col gap-3.5 px-5 py-[18px]"
    >
      <div className="flex items-start justify-between gap-3">
        <CardTitle id="whitelist-title" title="Whitelist" subtitle="Domains we never report" />
        <Link
          href="/whitelist"
          className="inline-flex min-h-10 items-center rounded-control border border-control-border bg-panel px-3.5 text-[13.5px] font-medium text-text no-underline shadow-xs hover:bg-segment hover:text-text hover:no-underline"
          aria-label="Manage whitelist"
        >
          Manage
        </Link>
      </div>
      <div className="flex flex-wrap gap-x-8 gap-y-2">
        <p className="flex flex-col">
          <span className="tabular text-[24px] leading-[30px] font-semibold tracking-[-0.02em]">
            {count(whitelist.total)}
          </span>
          <span className="text-[12.5px] text-text-tertiary">
            {whitelist.total === 1 ? "domain listed" : "domains listed"}
          </span>
        </p>
        <p className="flex flex-col">
          <span className="tabular text-[24px] leading-[30px] font-semibold tracking-[-0.02em]">
            {count(whitelist.added_this_month)}
          </span>
          <span className="text-[12.5px] text-text-tertiary">added this month</span>
        </p>
      </div>
      {whitelist.latest.length ? (
        <div className="flex flex-col">
          <p className="mb-1 text-[12px] leading-[18px] font-medium text-text-secondary">
            Recently added
          </p>
          <ul className="flex flex-col">
            {whitelist.latest.slice(0, LATEST_ROWS).map((entry) => (
              <li
                key={entry.id}
                className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 border-t border-row-divider py-2 last:pb-0"
              >
                <span className="min-w-0 font-mono text-[12.5px] font-medium [overflow-wrap:anywhere]">
                  {entry.pattern}
                </span>
                <span className="text-[12px] whitespace-nowrap text-text-tertiary">
                  {WHITELIST_KIND_LABELS[entry.kind]} · {monthDay(entry.created_at, timeZone)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}
