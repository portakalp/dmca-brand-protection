"use client";

import { Menu } from "lucide-react";
import { useSearchParams } from "next/navigation";
import type { CSSProperties, ReactNode } from "react";

import { ErrorState } from "@/components/states";
import { Skeleton } from "@/components/ui/skeleton";
import { SidebarInset, SidebarProvider, useSidebar } from "@/components/ui/sidebar";
import { useMe, useOverview } from "@/lib/api/queries";
import { rangeDays } from "@/lib/period";

import { AppSidebar } from "./app-sidebar";
import { Logo } from "./logo";
import { PanelProvider, pickBrand } from "./panel";
import { ReadOnlyBanner } from "./read-only-banner";

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

/** The phone's top bar: menu, wordmark and the live count. */
function TopBar({ live }: { live: number | null }) {
  const { toggleSidebar } = useSidebar();
  return (
    <div className="sticky top-0 z-20 flex items-center justify-between gap-3 border-b border-border bg-surface px-4 py-2 md:hidden">
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-label="Open menu"
          onClick={toggleSidebar}
          className="inline-flex size-9 items-center justify-center rounded-sm border border-border-strong text-text-2 hover:bg-sunken hover:text-text"
        >
          <Menu aria-hidden="true" strokeWidth={1.5} className="size-[18px]" />
        </button>
        <Logo />
      </div>
      {live ? (
        <span className="tabular inline-flex items-center gap-1.5 text-[13px] font-semibold text-st-critical">
          <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
          {live} live
        </span>
      ) : null}
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const me = useMe();
  const params = useSearchParams();
  const brand = me.data ? pickBrand(me.data, params.get("brand")) : null;
  const timeZone = me.data?.customer.timezone ?? null;
  const live = useLiveThreats(brand?.id ?? null, timeZone);

  if (me.isPending) {
    return (
      <div className="flex min-h-svh gap-6 p-5" aria-busy="true" aria-label="Loading">
        <Skeleton className="hidden h-[calc(100svh-2.5rem)] w-52 md:block" />
        <Skeleton className="h-[calc(100svh-2.5rem)] flex-1 rounded-lg" />
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
      <SidebarProvider style={SIDEBAR_STYLE} className="bg-bg">
        <AppSidebar liveThreats={live} />
        <SidebarInset className="min-w-0 bg-bg">
          <TopBar live={live} />
          <div
            id="main"
            className="flex w-full max-w-[1360px] flex-col gap-6 px-4 pt-5 pb-10 md:pt-7 md:pr-9 md:pb-12 md:pl-5"
          >
            <ReadOnlyBanner />
            {children}
          </div>
        </SidebarInset>
      </SidebarProvider>
    </PanelProvider>
  );
}
