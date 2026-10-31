"use client";

import {
  FileText,
  LayoutGrid,
  ListChecks,
  Settings,
  ShieldAlert,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import type { ReactNode } from "react";

import { Sidebar, useSidebar } from "@/components/ui/sidebar";
import { useWhitelist } from "@/lib/api/queries";
import { sharedQuery } from "@/lib/url";
import { cn } from "@/lib/utils";

import { BrandPicker } from "./brand-picker";
import { Logo } from "./logo";
import { usePanel } from "./panel";
import { ThemeButton, UserMenu } from "./user-menu";

type Item = { href: string; label: string; icon: LucideIcon };

const NAV: Item[] = [
  { href: "/overview", label: "Overview", icon: LayoutGrid },
  { href: "/threats", label: "Threats", icon: ShieldAlert },
  { href: "/whitelist", label: "Whitelist", icon: ListChecks },
  { href: "/reports", label: "Reports", icon: FileText },
  { href: "/settings", label: "Settings", icon: Settings },
];

/** "8 live" in the critical ink with a dot; read as "Threats, 8 live". */
function LiveCount({ count }: { count: number }) {
  return (
    <span className="tabular ml-auto inline-flex items-center gap-[5px] text-[12px] font-semibold text-st-critical">
      <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
      <span className="sr-only">, </span>
      {count} live
    </span>
  );
}

/** A plain count; the link's accessible name carries its unit (see NavItem's `label`). */
function PlainCount({ count }: { count: number }) {
  return (
    <span aria-hidden="true" className="tabular ml-auto text-[12px] font-medium text-text-3">
      {count}
    </span>
  );
}

function NavItem({ item, badge, label }: { item: Item; badge?: ReactNode; label?: string }) {
  const pathname = usePathname();
  const params = useSearchParams();
  const { setOpenMobile } = useSidebar();
  const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
  const Icon = item.icon;
  return (
    <li>
      <Link
        href={`${item.href}${sharedQuery(params)}`}
        aria-current={active ? "page" : undefined}
        aria-label={label}
        onClick={() => setOpenMobile(false)}
        className={cn(
          "flex min-h-9 items-center gap-2.5 border-l-2 border-transparent py-2 pr-2.5 pl-[18px] text-[14px] font-medium text-text-2 no-underline hover:bg-sunken hover:text-text hover:no-underline",
          active && "border-accent font-semibold text-text",
        )}
      >
        <Icon
          aria-hidden="true"
          strokeWidth={1.5}
          className={cn("size-4 flex-none", active && "text-accent")}
        />
        <span>{item.label}</span>
        {badge}
      </Link>
    </li>
  );
}

/**
 * The sidebar sits on the page background with no border: wordmark, the workspace row, the
 * navigation (the selected item has a 2px accent bar), and at the foot the demo note, the
 * theme switch and the signed-in user. On a phone the same content opens as a sheet.
 */
export function AppSidebar({ liveThreats }: { liveThreats: number | null }) {
  const { me } = usePanel();
  const whitelist = useWhitelist();
  const demoBrand = me.brands[0]?.name ?? me.customer.name;
  const listed = whitelist.data?.length ?? null;

  const badge = (href: string) => {
    if (href === "/threats" && liveThreats) return <LiveCount count={liveThreats} />;
    if (href === "/whitelist" && listed !== null) return <PlainCount count={listed} />;
    return null;
  };
  // Spoken as "Whitelist, 24 domains" without repeating the page's own "24 domains" text.
  const label = (item: Item) =>
    item.href === "/whitelist" && listed !== null
      ? `${item.label}, ${listed} ${listed === 1 ? "domain" : "domains"}`
      : undefined;

  return (
    <Sidebar variant="sidebar" className="group-data-[side=left]:border-r-0">
      <div className="flex h-full flex-col gap-[22px] overflow-y-auto pt-[22px] pr-4 pb-5 pl-5">
        <Logo />
        <BrandPicker />
        <nav aria-label="Primary" className="-ml-5">
          <ul className="flex flex-col gap-0.5">
            {NAV.map((item) => (
              <NavItem key={item.href} item={item} badge={badge(item.href)} label={label(item)} />
            ))}
          </ul>
        </nav>
        <div className="mt-auto flex flex-col gap-3.5 text-[12px] text-text-3">
          {me.customer.is_demo ? (
            <p>Demo workspace. Sample data for {demoBrand}, a fictional brand.</p>
          ) : null}
          <ThemeButton />
          <UserMenu />
        </div>
      </div>
    </Sidebar>
  );
}
