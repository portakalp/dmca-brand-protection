"use client";

import { Download, Search, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { Card, EmptyState, ErrorState, LoadingState, PanelHeader } from "@/components/states";
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
import { threatsParams, useThreats } from "@/lib/api/queries";
import { CATEGORY_LABELS, count, plural } from "@/lib/format";
import { PRESET_DAYS, parseRange, rangeDays, shortDay, zoneName, type Range } from "@/lib/period";
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

/** The lede's period: "in the last 30 days", "between Oct 2 and Oct 31", "since we started watching". */
function periodPhrase(range: Range): string {
  if (range.key === "all") return "since we started watching";
  if (range.key === "custom") return `between ${shortDay(range.from!)} and ${shortDay(range.to!)}`;
  return `in the last ${PRESET_DAYS[range.key]} days`;
}

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
    <div className="flex min-w-0 flex-[0_1_180px] flex-col max-sm:flex-[1_1_140px]">
      <Label htmlFor={id} className="sr-only">
        {label}
      </Label>
      <Select value={value ?? ANY} onValueChange={(next) => onChange(next === ANY ? null : next)}>
        <SelectTrigger id={id} aria-label={label} className="w-full">
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
        strokeWidth={1.5}
        className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-text-3"
      />
      <Input
        type="search"
        aria-label="Search domains"
        placeholder="Search domains"
        value={text}
        maxLength={200}
        onChange={(event) => setText(event.target.value)}
        className="pl-9"
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
        subtitle={
          data
            ? `${count(data.total)} lookalike ${plural(data.total, "domain", "domains")} found ${periodPhrase(range)}. Times are in ${zoneName(timeZone)}.`
            : `Every lookalike domain we found. Times are in ${zoneName(timeZone)}.`
        }
        range={{ value: range, fallback: FALLBACK }}
      />
      <Card aria-labelledby="threats-title" className="flex flex-col overflow-hidden">
        <PanelHeader
          id="threats-title"
          title="All threats"
          description="Newest first. Open a row to see its evidence and takedown progress."
          actions={
            <Button asChild variant="outline">
              <a href={csv} download>
                <Download aria-hidden="true" />
                Download CSV
              </a>
            </Button>
          }
        />
        <div
          role="search"
          aria-label="Filter threats"
          className="flex flex-wrap items-center gap-2 px-5 pt-4 pb-4 max-sm:px-4"
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
              onClick={() => update({ q: null, status: null, category: null, page: null })}
            >
              <X aria-hidden="true" />
              Clear filters
            </Button>
          ) : null}
        </div>
        {threats.isPending ? (
          <LoadingState rows={8} label="Loading threats" className="border-t border-border" />
        ) : threats.isError && !data ? (
          <ErrorState
            error={threats.error}
            onRetry={() => threats.refetch()}
            className="border-t border-border"
          />
        ) : data && data.items.length === 0 ? (
          hasFilters(filters) || range.key !== FALLBACK ? (
            <EmptyState title="No threats match these filters" className="border-t border-border">
              Try another status, type or period.
            </EmptyState>
          ) : (
            <EmptyState title="No threats yet" className="border-t border-border">
              We&apos;re watching new domains around the clock. Lookalikes of your brand show up
              here as soon as we find them.
            </EmptyState>
          )
        ) : data ? (
          <div
            className="transition-opacity aria-busy:opacity-60 motion-reduce:transition-none"
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
              className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-5 py-3 text-[13px] text-text-2 max-sm:px-4"
            >
              <span className="tabular" role="status">
                {showing(data.page, data.total)}
              </span>
              <span className="flex items-center gap-2">
                <span className="tabular">
                  Page {data.page} of {pages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={data.page <= 1}
                  onClick={() => update({ page: data.page - 1 > 1 ? String(data.page - 1) : null })}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
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
