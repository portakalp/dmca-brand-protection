"use client";

import { Download, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { SimpleTable, Td, Th, Tr } from "@/components/simple-table";
import { Card, EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { accessOf } from "@/lib/access";
import type { Schemas } from "@/lib/api/client";
import { useWhitelist } from "@/lib/api/queries";
import { WHITELIST_KIND_LABELS, count, plural } from "@/lib/format";
import { longDay, todayIn } from "@/lib/period";

import { AddEntry, DeleteEntry, ImportCsv } from "./whitelist-dialogs";

const SOURCE_LABELS: Record<Schemas["WhitelistSource"], string> = {
  manual: "Added by hand",
  csv: "CSV import",
  onboarding: "Onboarding",
  this_is_ours: "This is our site",
};

export function WhitelistView() {
  const { me, timeZone } = usePanel();
  const access = accessOf(me);
  const whitelist = useWhitelist();
  const [query, setQuery] = useState("");
  const brands = new Map(me.brands.map((brand) => [brand.id, brand.name]));
  const showBrand = me.brands.length > 1;

  const entries = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = whitelist.data ?? [];
    return q
      ? all.filter(
          (entry) => entry.pattern.includes(q) || (entry.note ?? "").toLowerCase().includes(q),
        )
      : all;
  }, [whitelist.data, query]);

  return (
    <>
      <PageHeader
        title="Whitelist"
        subtitle="Domains Kalkan never reports. Each entry covers the domain and its subdomains."
      />
      <Card aria-label="Whitelist" className="flex flex-col overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b border-card-border px-5 py-4">
          <div className="relative min-w-0 flex-[1_1_220px]">
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-text-tertiary"
            />
            <Input
              type="search"
              aria-label="Search the whitelist"
              placeholder="Search domains and notes"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="min-h-10 rounded-control border-control-border bg-panel pl-9 text-[13.5px] shadow-xs"
            />
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {access.owner ? (
              <Button asChild variant="control" size="control">
                <a
                  href="/api/v1/whitelist/export"
                  download
                  className="text-text no-underline hover:text-text hover:no-underline"
                >
                  <Download aria-hidden="true" className="text-text-secondary" />
                  Export CSV
                </a>
              </Button>
            ) : null}
            <ImportCsv mode={access.whitelist} />
            <AddEntry mode={access.whitelist} />
          </div>
        </div>

        {whitelist.isPending ? (
          <div
            aria-busy="true"
            aria-label="Loading the whitelist"
            className="flex flex-col gap-2 p-5"
          >
            {Array.from({ length: 6 }, (_, index) => (
              <Skeleton key={index} className="h-11" />
            ))}
          </div>
        ) : whitelist.isError ? (
          <ErrorState error={whitelist.error} onRetry={() => whitelist.refetch()} />
        ) : whitelist.data.length === 0 ? (
          <EmptyState title="Your whitelist is empty">
            Add your official domains, alternate domains and partners so they are never reported.
          </EmptyState>
        ) : entries.length === 0 ? (
          <EmptyState title="No matches">No domain or note matches “{query.trim()}”.</EmptyState>
        ) : (
          <>
            <SimpleTable caption="Whitelisted domains" minWidth={680}>
              <thead>
                <tr>
                  <Th>Domain</Th>
                  <Th>Kind</Th>
                  {showBrand ? <Th>Brand</Th> : null}
                  <Th>Note</Th>
                  <Th>Added by</Th>
                  <Th>Added</Th>
                  {access.whitelist !== "hidden" ? (
                    <Th>
                      <span className="sr-only">Actions</span>
                    </Th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <Tr key={entry.id}>
                    <Td className="font-mono text-[12.5px] font-medium text-text">
                      {entry.pattern}
                    </Td>
                    <Td className="whitespace-nowrap">{WHITELIST_KIND_LABELS[entry.kind]}</Td>
                    {showBrand ? (
                      <Td className="whitespace-nowrap">
                        {entry.brand_id ? (brands.get(entry.brand_id) ?? "—") : "Every brand"}
                      </Td>
                    ) : null}
                    <Td className="max-w-[260px] truncate">{entry.note ?? "—"}</Td>
                    <Td className="whitespace-nowrap">
                      {entry.created_by ?? SOURCE_LABELS[entry.source]}
                    </Td>
                    <Td className="tabular whitespace-nowrap">
                      {longDay(todayIn(timeZone, new Date(entry.created_at)))}
                    </Td>
                    {access.whitelist !== "hidden" ? (
                      <Td className="w-12 py-1 text-right">
                        <DeleteEntry entry={entry} mode={access.whitelist} />
                      </Td>
                    ) : null}
                  </Tr>
                ))}
              </tbody>
            </SimpleTable>
            <p className="tabular border-t border-card-border px-5 py-3 text-[13px] text-text-secondary">
              {entries.length === whitelist.data.length
                ? `${count(entries.length)} ${plural(entries.length, "domain", "domains")}`
                : `${count(entries.length)} of ${count(whitelist.data.length)} domains`}
            </p>
          </>
        )}
      </Card>
    </>
  );
}
