"use client";

import { Download, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { SimpleTable, Td, Th, Tr } from "@/components/simple-table";
import { Card, EmptyState, ErrorState, LoadingState, PanelHeader } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  this_is_ours: "Marked as ours",
};

export function WhitelistView() {
  const { me, timeZone } = usePanel();
  const access = accessOf(me);
  const whitelist = useWhitelist();
  const [query, setQuery] = useState("");
  const brands = new Map(me.brands.map((brand) => [brand.id, brand.name]));
  const showBrand = me.brands.length > 1;
  const writes = access.whitelist !== "hidden";

  const entries = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = whitelist.data ?? [];
    return q
      ? all.filter(
          (entry) => entry.pattern.includes(q) || (entry.note ?? "").toLowerCase().includes(q),
        )
      : all;
  }, [whitelist.data, query]);

  const thisMonth = useMemo(() => {
    const month = todayIn(timeZone).slice(0, 7);
    return (whitelist.data ?? []).filter(
      (entry) => todayIn(timeZone, new Date(entry.created_at)).slice(0, 7) === month,
    ).length;
  }, [whitelist.data, timeZone]);

  return (
    <>
      <PageHeader
        title="Whitelist"
        subtitle="Your own domains and partners. Kalkan never reports these or their subdomains."
        actions={
          <>
            {access.owner ? (
              <Button asChild variant="outline">
                <a href="/api/v1/whitelist/export" download>
                  <Download aria-hidden="true" />
                  Export CSV
                </a>
              </Button>
            ) : null}
            <ImportCsv mode={access.whitelist} />
            <AddEntry mode={access.whitelist} />
          </>
        }
      />
      <Card aria-labelledby="whitelist-title" className="flex flex-col overflow-hidden">
        <PanelHeader
          id="whitelist-title"
          title="Listed domains"
          description={
            whitelist.data
              ? `Each entry covers the domain and all its subdomains. ${count(thisMonth)} added this month.`
              : "Each entry covers the domain and all its subdomains."
          }
        />
        <div className="flex px-5 pt-4 pb-4 max-sm:px-4">
          <div className="relative min-w-0 flex-[0_1_360px] max-sm:flex-1">
            <Search
              aria-hidden="true"
              strokeWidth={1.5}
              className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-text-3"
            />
            <Input
              type="search"
              aria-label="Search the whitelist"
              placeholder="Search domains and notes"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="pl-9"
            />
          </div>
        </div>

        {whitelist.isPending ? (
          <LoadingState rows={6} label="Loading the whitelist" className="border-t border-border" />
        ) : whitelist.isError ? (
          <ErrorState
            error={whitelist.error}
            onRetry={() => whitelist.refetch()}
            className="border-t border-border"
          />
        ) : whitelist.data.length === 0 ? (
          <EmptyState title="Your whitelist is empty" className="border-t border-border">
            Add your official domains, alternate domains and partners so they are never reported.
          </EmptyState>
        ) : entries.length === 0 ? (
          <EmptyState title="No matches" icon={Search} className="border-t border-border">
            No domain or note matches “{query.trim()}”.
          </EmptyState>
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
                  {writes ? (
                    <Th className="w-12">
                      <span className="sr-only">Actions</span>
                    </Th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <Tr key={entry.id}>
                    <Td primary className="max-w-[280px]">
                      <span
                        title={entry.pattern}
                        className="block truncate font-mono text-[13.5px] font-medium text-text"
                      >
                        {entry.pattern}
                      </span>
                    </Td>
                    <Td label="Kind" className="whitespace-nowrap">
                      {WHITELIST_KIND_LABELS[entry.kind]}
                    </Td>
                    {showBrand ? (
                      <Td label="Brand" className="whitespace-nowrap">
                        {entry.brand_id ? (brands.get(entry.brand_id) ?? "—") : "Every brand"}
                      </Td>
                    ) : null}
                    <Td label="Note" className="max-w-[260px] truncate">
                      {entry.note ?? <span className="text-text-3">—</span>}
                    </Td>
                    <Td label="Added by" className="whitespace-nowrap">
                      {entry.created_by ?? SOURCE_LABELS[entry.source]}
                    </Td>
                    <Td label="Added" className="tabular whitespace-nowrap">
                      {longDay(todayIn(timeZone, new Date(entry.created_at)))}
                    </Td>
                    {writes ? (
                      <Td action className="py-1.5 text-right">
                        <DeleteEntry entry={entry} mode={access.whitelist} />
                      </Td>
                    ) : null}
                  </Tr>
                ))}
              </tbody>
            </SimpleTable>
            <p className="tabular border-t border-border px-5 py-3 text-[13px] text-text-2 max-sm:px-4">
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
