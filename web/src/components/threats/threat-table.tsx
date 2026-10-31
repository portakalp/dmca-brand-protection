"use client";

import { createColumnHelper, flexRender, tableFeatures, useTable } from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronRight } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo } from "react";

import { usePanel } from "@/components/shell/panel";
import { Status } from "@/components/status-badge";
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

/** Sentence-case header cells on the sunken band. */
const TH =
  "border-y border-border bg-sunken px-3 py-[9px] text-left text-[12.5px] font-semibold whitespace-nowrap text-text-2 first:pl-5";

/**
 * Column classes, shared by the header and the cells. Hosted by drops out first on narrow
 * screens. At 560px and below the same table becomes stacked rows: the domain on its own line,
 * then status on the left and first seen on the right; last action drops out (the detail page has
 * it). The explicit roles keep the table semantics when the rows become grids.
 */
const COLUMN: Record<string, { th?: string; td: string }> = {
  domain: {
    td: "max-w-[300px] max-[1100px]:max-w-[240px] max-[560px]:col-span-2 max-[560px]:max-w-none",
  },
  status: { td: "whitespace-nowrap max-[560px]:col-start-1" },
  first_seen_at: {
    td: "tabular whitespace-nowrap max-[560px]:col-start-2 max-[560px]:text-right max-[560px]:text-[13px] max-[560px]:text-text-3",
  },
  hosting: { th: "max-[860px]:hidden", td: "whitespace-nowrap max-[860px]:hidden!" },
  last_action: { th: "max-[560px]:hidden", td: "min-w-[150px] max-[560px]:hidden!" },
};

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
            const unicode =
              threat.domain_unicode && threat.domain_unicode !== threat.domain
                ? ` (${threat.domain_unicode})`
                : "";
            return (
              <>
                <Link
                  href={threatHref(threat)}
                  title={`${threat.domain}${unicode}`}
                  className="block truncate font-mono text-[13.5px] font-medium text-text underline decoration-border-strong underline-offset-4 group-hover:text-accent group-hover:decoration-accent hover:text-accent hover:decoration-accent"
                >
                  {threat.domain}
                </Link>
                <span className="mt-0.5 block truncate text-[12.5px] text-text-3">
                  <span className="font-mono text-[12px]">{threat.number}</span>
                  {" · "}
                  {categoryLabel(threat.category)}
                </span>
              </>
            );
          },
        }),
        column.accessor("status", {
          header: "Status",
          cell: (info) => <Status status={info.getValue()} />,
        }),
        column.accessor("first_seen_at", {
          header: "First seen",
          cell: (info) => (
            <>
              <span className="hidden max-[560px]:inline">First seen </span>
              {firstSeen(info.getValue(), timeZone)}
            </>
          ),
        }),
        column.accessor("hosting", {
          header: "Hosted by",
          cell: (info) => info.getValue() ?? <span className="text-text-3">Not known yet</span>,
        }),
        column.accessor("last_action", {
          header: "Last action",
          cell: (info) => {
            const text = info.getValue();
            return text ? (
              lastAction(text, info.row.original.last_action_at, timeZone)
            ) : (
              <span className="text-text-3">No action yet</span>
            );
          },
        }),
      ]),
    [timeZone],
  );
  const table = useTable({ features, columns, data: threats });

  return (
    <table role="table" className="w-full border-collapse text-[13.5px] max-[560px]:block">
      <caption className="sr-only">{caption}</caption>
      <thead role="rowgroup" className="max-[560px]:sr-only">
        {table.getHeaderGroups().map((group) => (
          <tr key={group.id} role="row">
            {group.headers.map((header) => {
              const sortable = onSort && SORTABLE[header.column.id];
              const dir = direction(header.column.id, sort);
              const label = flexRender(header.column.columnDef.header, header.getContext());
              const Icon = dir === "asc" ? ArrowUp : dir === "desc" ? ArrowDown : ArrowUpDown;
              return (
                <th
                  key={header.id}
                  scope="col"
                  role="columnheader"
                  aria-sort={
                    dir === "asc" ? "ascending" : dir === "desc" ? "descending" : undefined
                  }
                  className={cn(TH, COLUMN[header.column.id]?.th)}
                >
                  {sortable ? (
                    <button
                      type="button"
                      onClick={() => onSort(nextSort(header.column.id, sort))}
                      className="-mx-1 inline-flex items-center gap-1 rounded-xs px-1 hover:text-text"
                    >
                      {label}
                      <Icon
                        aria-hidden="true"
                        strokeWidth={1.5}
                        className={cn("size-3.5", dir ? "text-text" : "text-text-3")}
                      />
                    </button>
                  ) : (
                    label
                  )}
                </th>
              );
            })}
            <th scope="col" role="columnheader" className={cn(TH, "w-9 pr-5 pl-0")}>
              <span className="sr-only">Open</span>
            </th>
          </tr>
        ))}
      </thead>
      <tbody role="rowgroup" className="max-[560px]:block">
        {table.getRowModel().rows.map((row) => (
          <tr
            key={row.id}
            role="row"
            className="group cursor-pointer border-b border-border last:border-b-0 hover:bg-sunken max-[560px]:grid max-[560px]:grid-cols-[minmax(0,1fr)_auto] max-[560px]:items-center max-[560px]:gap-x-3 max-[560px]:gap-y-2 max-[560px]:px-4 max-[560px]:py-3"
            // The domain link is the keyboard path; the whole row is a pointer target.
            onClick={(event) => {
              if ((event.target as HTMLElement).closest("a, button")) return;
              router.push(threatHref(row.original));
            }}
          >
            {row.getAllCells().map((cell) => (
              <td
                key={cell.id}
                role="cell"
                className={cn(
                  "px-3 py-[11px] align-top text-text-2 first:pl-5 max-[560px]:block max-[560px]:min-w-0 max-[560px]:p-0 max-[560px]:first:p-0",
                  COLUMN[cell.column.id]?.td,
                )}
              >
                {flexRender(cell.column.columnDef.cell, cell.getContext())}
              </td>
            ))}
            <td
              role="cell"
              className="w-9 py-[11px] pr-5 pl-0 align-middle text-text-3 group-hover:text-accent max-[560px]:hidden"
            >
              <ChevronRight aria-hidden="true" strokeWidth={1.5} className="size-4" />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
