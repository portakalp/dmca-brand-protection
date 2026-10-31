"use client";

import { createColumnHelper, flexRender, tableFeatures, useTable } from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo } from "react";

import { usePanel } from "@/components/shell/panel";
import { StatusBadge } from "@/components/status-badge";
import type { Schemas } from "@/lib/api/client";
import type { ThreatSort } from "@/lib/api/queries";
import { categoryLabel, firstSeen, lastAction } from "@/lib/format";
import { cn } from "@/lib/utils";

type Threat = Schemas["ThreatItem"];

// Sorting and paging happen in the API; the table renders one page.
const features = tableFeatures({});
const column = createColumnHelper<typeof features, Threat>();

/** Which API sort a column header switches to, and back. */
const SORTABLE: Record<string, { asc?: ThreatSort; desc?: ThreatSort }> = {
  domain: { asc: "domain" },
  first_seen_at: { desc: "-first_seen_at", asc: "first_seen_at" },
  last_action: { desc: "-last_action_at" },
};

function direction(columnId: string, sort: ThreatSort | undefined): "asc" | "desc" | null {
  const option = SORTABLE[columnId];
  if (!option || !sort) return null;
  if (option.asc === sort) return "asc";
  if (option.desc === sort) return "desc";
  return null;
}

function nextSort(columnId: string, sort: ThreatSort | undefined): ThreatSort {
  const option = SORTABLE[columnId];
  const current = direction(columnId, sort);
  if (current === "desc" && option.asc) return option.asc;
  if (current === "asc" && option.desc) return option.desc;
  return (option.desc ?? option.asc)!;
}

export function threatHref(threat: Threat): string {
  return `/threats/${threat.id}`;
}

export function ThreatTable({
  threats,
  sort,
  onSort,
  caption,
}: {
  threats: Threat[];
  sort?: ThreatSort;
  onSort?: (sort: ThreatSort) => void;
  caption: string;
}) {
  const { timeZone } = usePanel();
  const router = useRouter();
  const columns = useMemo(
    () =>
      column.columns([
        column.accessor("domain", {
          header: "Domain",
          cell: (info) => {
            const threat = info.row.original;
            return (
              <>
                <Link
                  href={threatHref(threat)}
                  className="font-mono text-[13px] font-medium whitespace-nowrap text-text hover:text-link"
                  title={threat.domain_unicode ?? undefined}
                >
                  {threat.domain}
                </Link>
                <span className="block text-[12px] text-text-tertiary">
                  {categoryLabel(threat.category)}
                </span>
              </>
            );
          },
        }),
        column.accessor("status", {
          header: "Status",
          cell: (info) => <StatusBadge status={info.getValue()} />,
        }),
        column.accessor("first_seen_at", {
          header: "First seen",
          cell: (info) => firstSeen(info.getValue(), timeZone),
        }),
        column.accessor("hosting", {
          header: "Hosting",
          cell: (info) => info.getValue() ?? "—",
        }),
        column.accessor("last_action", {
          header: "Last action",
          cell: (info) => lastAction(info.getValue(), info.row.original.last_action_at, timeZone),
        }),
      ]),
    [timeZone],
  );
  const table = useTable({ features, columns, data: threats });

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[660px] border-collapse text-[13.5px]">
        <caption className="sr-only">{caption}</caption>
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id} className="bg-table-head">
              {group.headers.map((header) => {
                const sortable = onSort && SORTABLE[header.column.id];
                const dir = direction(header.column.id, sort);
                const label = flexRender(header.column.columnDef.header, header.getContext());
                const Icon = dir === "asc" ? ArrowUp : dir === "desc" ? ArrowDown : ArrowUpDown;
                return (
                  <th
                    key={header.id}
                    scope="col"
                    aria-sort={
                      dir === "asc" ? "ascending" : dir === "desc" ? "descending" : undefined
                    }
                    className="border-y border-card-border px-3 py-2.5 text-left text-[12px] font-medium whitespace-nowrap text-text-secondary first:pl-5 last:pr-5"
                  >
                    {sortable ? (
                      <button
                        type="button"
                        onClick={() => onSort(nextSort(header.column.id, sort))}
                        className="-mx-1 inline-flex items-center gap-1 rounded px-1 hover:text-text"
                      >
                        {label}
                        <Icon
                          aria-hidden="true"
                          className={cn("size-3.5", !dir && "text-text-tertiary/70")}
                        />
                      </button>
                    ) : (
                      label
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              className="cursor-pointer border-row-divider not-first:border-t hover:bg-table-head"
              // The domain link is the keyboard path; the whole row is a pointer target.
              onClick={(event) => {
                if ((event.target as HTMLElement).closest("a, button")) return;
                router.push(threatHref(row.original));
              }}
            >
              {row.getAllCells().map((cell) => (
                <td
                  key={cell.id}
                  className={cn(
                    "px-3 py-[11px] align-middle text-text-secondary first:pl-5 last:pr-5",
                    cell.column.id !== "domain" &&
                      cell.column.id !== "last_action" &&
                      "whitespace-nowrap",
                  )}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
