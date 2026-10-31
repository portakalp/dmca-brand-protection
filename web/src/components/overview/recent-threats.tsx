"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { usePanel } from "@/components/shell/panel";
import { Status } from "@/components/status-badge";
import { threatHref } from "@/components/threats/threat-table";
import type { Schemas } from "@/lib/api/client";
import { categoryLabel, firstSeen, lastAction } from "@/lib/format";
import { cn } from "@/lib/utils";

type Threat = Schemas["ThreatItem"];

// At 560px and below each row stacks: the domain on its own line, then status and first seen.
const HEAD =
  "border-y border-border bg-sunken px-3 py-[9px] text-left text-[12.5px] font-semibold whitespace-nowrap text-text-2 first:pl-5";
const CELL = "border-b border-border px-3 py-[11px] align-top first:pl-5";

/**
 * The newest threats as a table. The domain link is the keyboard path; the whole row is a
 * pointer target. On a phone the same table renders as stacked rows, so nothing scrolls sideways.
 */
export function RecentThreats({ threats }: { threats: Threat[] }) {
  const { timeZone } = usePanel();
  const router = useRouter();
  return (
    <table className="w-full table-fixed border-collapse text-[13.5px] max-[560px]:table-auto">
      <caption className="sr-only">Recent threats, newest first</caption>
      <colgroup className="max-[560px]:hidden">
        <col className="w-[32%]" />
        <col className="w-[15%]" />
        <col className="w-[13%]" />
        <col className="w-[14%]" />
        <col />
        <col className="w-11" />
      </colgroup>
      <thead className="max-[560px]:sr-only">
        <tr>
          <th scope="col" className={HEAD}>
            Domain
          </th>
          <th scope="col" className={HEAD}>
            Status
          </th>
          <th scope="col" className={HEAD}>
            First seen
          </th>
          <th scope="col" className={HEAD}>
            Hosted by
          </th>
          <th scope="col" className={HEAD}>
            Last action
          </th>
          <th scope="col" className={HEAD}>
            <span className="sr-only">Open</span>
          </th>
        </tr>
      </thead>
      <tbody className="max-[560px]:block">
        {threats.map((threat) => (
          <tr
            key={threat.id}
            role="row"
            className={cn(
              "group cursor-pointer hover:bg-sunken [&:last-child>td]:border-b-0 max-[560px]:last:border-b-0",
              "max-[560px]:grid max-[560px]:grid-cols-[minmax(0,1fr)_auto] max-[560px]:gap-x-3 max-[560px]:gap-y-1 max-[560px]:border-b max-[560px]:border-border max-[560px]:px-4 max-[560px]:py-3",
            )}
            onClick={(event) => {
              if ((event.target as HTMLElement).closest("a, button")) return;
              router.push(threatHref(threat));
            }}
          >
            <td
              role="cell"
              className={cn(
                CELL,
                // `first:pl-5` outranks a plain `p-0`, so the stacked row resets it explicitly.
                "max-[560px]:col-span-2 max-[560px]:border-0 max-[560px]:p-0 max-[560px]:first:pl-0",
              )}
            >
              <Link
                href={threatHref(threat)}
                title={threat.domain_unicode ?? threat.domain}
                className="block truncate font-mono text-[13.5px] font-medium text-text underline decoration-border-strong underline-offset-4 group-hover:text-accent group-hover:decoration-accent"
              >
                {threat.domain}
              </Link>
              <span className="mt-0.5 block truncate text-[12.5px] text-text-3">
                <span className="font-mono text-[12px]">{threat.number}</span> ·{" "}
                {categoryLabel(threat.category)}
              </span>
            </td>
            <td role="cell" className={cn(CELL, "max-[560px]:border-0 max-[560px]:p-0")}>
              <Status status={threat.status} />
            </td>
            <td
              role="cell"
              className={cn(
                CELL,
                "tabular whitespace-nowrap text-text-2",
                "max-[560px]:border-0 max-[560px]:p-0 max-[560px]:text-right max-[560px]:text-[13px]",
              )}
            >
              <span className="hidden max-[560px]:inline">First seen </span>
              {firstSeen(threat.first_seen_at, timeZone)}
            </td>
            <td
              role="cell"
              className={cn(CELL, "truncate text-text-2 max-[560px]:hidden")}
              title={threat.hosting ?? undefined}
            >
              {threat.hosting ?? "—"}
            </td>
            <td role="cell" className={cn(CELL, "text-text-2 max-[560px]:hidden")}>
              {lastAction(threat.last_action, threat.last_action_at, timeZone)}
            </td>
            <td
              role="cell"
              className={cn(
                CELL,
                "w-11 pr-5 pl-0 text-text-3 group-hover:text-accent max-[560px]:hidden",
              )}
            >
              <ChevronRight aria-hidden="true" strokeWidth={1.5} className="mt-0.5 size-4" />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
