"use client";

import { useSearchParams } from "next/navigation";
import type { CSSProperties, ReactNode } from "react";

import { ErrorState } from "@/components/states";
import { Skeleton } from "@/components/ui/skeleton";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { useMe, useOverview } from "@/lib/api/queries";
import { rangeDays } from "@/lib/period";

import { AppSidebar } from "./app-sidebar";
import { ReadOnlyBanner } from "./read-only-banner";
import { Logo } from "./logo";
import { PanelProvider, pickBrand } from "./panel";

const SIDEBAR_STYLE = { "--sidebar-width": "15rem" } as CSSProperties;

/** The live count of the Threats badge; the overview's default period shares its query. */
function useLiveThreats(brand: string | null, timeZone: string | null) {
  const days = timeZone ? rangeDays({ key: "30d", from: null, to: null }, timeZone) : null;
  const overview = useOverview(
    { brand, from: days?.from ?? null, to: days?.to ?? null },
    timeZone !== null,
  );
  return overview.data?.live_threats ?? null;
}

export function AppShell({ children }: { children: ReactNode }) {
  const me = useMe();
  const params = useSearchParams();
  const brand = me.data ? pickBrand(me.data, params.get("brand")) : null;
  const timeZone = me.data?.customer.timezone ?? null;
  const live = useLiveThreats(brand?.id ?? null, timeZone);

  if (me.isPending) {
    return (
      <div className="flex min-h-svh gap-2 p-2" aria-busy="true" aria-label="Loading">
        <Skeleton className="hidden h-[calc(100svh-1rem)] w-56 md:block" />
        <Skeleton className="h-[calc(100svh-1rem)] flex-1 rounded-panel" />
      </div>
    );
  }
  if (me.isError) {
    return (
      <main className="flex min-h-svh items-center justify-center p-4">
        <ErrorState error={me.error} onRetry={() => me.refetch()} />
      </main>
    );
  }

  return (
    <PanelProvider value={{ me: me.data, timeZone: me.data.customer.timezone, brand }}>
      <SidebarProvider style={SIDEBAR_STYLE} className="bg-canvas">
        <AppSidebar liveThreats={live} />
        <SidebarInset className="min-w-0 border border-panel-border bg-panel shadow-panel max-md:m-2 max-md:rounded-panel md:rounded-panel">
          <div className="flex items-center gap-1 border-b border-row-divider px-2 py-1 md:hidden">
            <SidebarTrigger aria-label="Open menu" className="size-10" />
            <Logo />
          </div>
          <div
            id="main"
            className="mx-auto flex w-full max-w-[1180px] flex-col gap-5 px-4 pt-5 pb-9 sm:px-7 sm:pt-6"
          >
            <ReadOnlyBanner />
            {children}
          </div>
        </SidebarInset>
      </SidebarProvider>
    </PanelProvider>
  );
}
