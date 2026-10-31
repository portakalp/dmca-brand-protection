"use client";

import { Download, Search, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { Card, EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { threatsParams, useThreats } from "@/lib/api/queries";
import { CATEGORY_LABELS } from "@/lib/format";
import { parseRange, rangeDays, rangeLabel } from "@/lib/period";
import { STATUSES, STATUS_LABELS } from "@/lib/status";
import {
  DEFAULT_SORT,
  PAGE_SIZE,
  hasFilters,
  pageCount,
  parseFilters,
  showing,
} from "@/lib/threat-filters";
import { useUrlState, withParams } from "@/lib/url";

import { ThreatTable } from "./threat-table";

const FALLBACK = "all" as const;
const ANY = "any";

function FilterSelect({
  label,
  value,
  options,
  anyLabel,
  onChange,
}: {
  label: string;
  value: string | null;
  options: [string, string][];
  anyLabel: string;
  onChange: (value: string | null) => void;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id} className="sr-only">
        {label}
      </Label>
      <Select value={value ?? ANY} onValueChange={(next) => onChange(next === ANY ? null : next)}>
        <SelectTrigger
          id={id}
          aria-label={label}
          className="min-h-10 min-w-40 rounded-control border-control-border bg-panel text-[13.5px] shadow-xs"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>{anyLabel}</SelectItem>
          {options.map(([key, text]) => (
            <SelectItem key={key} value={key}>
              {text}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/** The search box writes to the URL a moment after typing stops. */
function SearchBox({ value, onChange }: { value: string; onChange: (q: string) => void }) {
  const [text, setText] = useState(value);
  // A new value from the URL (back button, Clear filters) replaces what was typed.
  const [synced, setSynced] = useState(value);
  if (value !== synced) {
    setSynced(value);
    setText(value);
  }
  useEffect(() => {
    if (text.trim() === value) return;
    const timer = setTimeout(() => onChange(text.trim()), 300);
    return () => clearTimeout(timer);
  }, [text, value, onChange]);
  return (
    <div className="relative min-w-0 flex-[1_1_240px]">
      <Search
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-text-tertiary"
      />
      <Input
        type="search"
        aria-label="Search domains"
        placeholder="Search domains"
        value={text}
        maxLength={200}
        onChange={(event) => setText(event.target.value)}
        className="min-h-10 rounded-control border-control-border bg-panel pl-9 text-[13.5px] shadow-xs"
      />
    </div>
  );
}

export function ThreatsView() {
  const { brand, timeZone } = usePanel();
  const { params, update } = useUrlState();
  const range = parseRange(params, FALLBACK);
  const days = rangeDays(range, timeZone);
  const filters = parseFilters(params);
  const query = {
    brand: brand?.id ?? null,
    from: days?.from ?? null,
    to: days?.to ?? null,
    ...filters,
    pageSize: PAGE_SIZE,
  };
  const threats = useThreats(query);
  const data = threats.data;
  const pages = data ? pageCount(data.total) : 1;
  const csv = `/api/v1/threats${withParams("", {
    ...Object.fromEntries(
      Object.entries(threatsParams(query)).map(([key, value]) => [key, value ?? null]),
    ),
    format: "csv",
  })}`;

  return (
    <>
      <PageHeader
        title="Threats"
        subtitle={data ? `${data.total} threats · ${rangeLabel(range)}` : rangeLabel(range)}
        range={{ value: range, fallback: FALLBACK }}
      />
      <Card aria-label="Threats" className="flex flex-col overflow-hidden">
        <div
          role="search"
          className="flex flex-wrap items-center gap-2 border-b border-card-border px-5 py-4"
        >
          <SearchBox value={filters.q ?? ""} onChange={(q) => update({ q, page: null })} />
          <FilterSelect
            label="Status"
            anyLabel="All statuses"
            value={filters.status}
            options={STATUSES.map((status) => [status, STATUS_LABELS[status]])}
            onChange={(status) => update({ status, page: null })}
          />
          <FilterSelect
            label="Type"
            anyLabel="All types"
            value={filters.category}
            options={Object.entries(CATEGORY_LABELS)}
            onChange={(category) => update({ category, page: null })}
          />
          {hasFilters(filters) ? (
            <Button
              variant="ghost"
              size="control"
              onClick={() => update({ q: null, status: null, category: null, page: null })}
            >
              <X aria-hidden="true" />
              Clear filters
            </Button>
          ) : null}
          <Button asChild variant="control" size="control" className="ml-auto">
            <a
              href={csv}
              download
              className="text-text no-underline hover:text-text hover:no-underline"
            >
              <Download aria-hidden="true" className="text-text-secondary" />
              Download CSV
            </a>
          </Button>
        </div>
        {threats.isPending ? (
          <div aria-busy="true" aria-label="Loading threats" className="flex flex-col gap-2 p-5">
            {Array.from({ length: 8 }, (_, index) => (
              <Skeleton key={index} className="h-11" />
            ))}
          </div>
        ) : threats.isError && !data ? (
          <ErrorState error={threats.error} onRetry={() => threats.refetch()} />
        ) : data && data.items.length === 0 ? (
          hasFilters(filters) || range.key !== FALLBACK ? (
            <EmptyState title="No threats match these filters">
              Try another status, type or date range.
            </EmptyState>
          ) : (
            <EmptyState title="No threats yet">
              We&apos;re watching new domains around the clock. Lookalikes of your brand show up
              here as soon as we find them.
            </EmptyState>
          )
        ) : data ? (
          <div
            className="transition-opacity aria-busy:opacity-60"
            aria-busy={threats.isPlaceholderData || undefined}
          >
            <ThreatTable
              threats={data.items}
              sort={filters.sort}
              onSort={(sort) => update({ sort: sort === DEFAULT_SORT ? null : sort, page: null })}
              caption="Threats"
            />
            <nav
              aria-label="Pagination"
              className="flex flex-wrap items-center justify-between gap-3 border-t border-card-border px-5 py-3 text-[13px] text-text-secondary"
            >
              <span className="tabular" role="status">
                {showing(data.page, data.total)}
              </span>
              <span className="flex items-center gap-2">
                <span className="tabular">
                  Page {data.page} of {pages}
                </span>
                <Button
                  variant="control"
                  size="control"
                  disabled={data.page <= 1}
                  onClick={() => update({ page: data.page - 1 > 1 ? String(data.page - 1) : null })}
                >
                  Previous
                </Button>
                <Button
                  variant="control"
                  size="control"
                  disabled={data.page >= pages}
                  onClick={() => update({ page: String(data.page + 1) })}
                >
                  Next
                </Button>
              </span>
            </nav>
          </div>
        ) : null}
      </Card>
    </>
  );
}
