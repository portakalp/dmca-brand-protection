"use client";

import {
  FileText,
  LayoutDashboard,
  ListChecks,
  Settings,
  ShieldAlert,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar";
import { sharedQuery } from "@/lib/url";

import { Logo } from "./logo";
import { usePanel } from "./panel";

type Item = { href: string; label: string; icon: LucideIcon };

const PRIMARY: Item[] = [
  { href: "/overview", label: "Overview", icon: LayoutDashboard },
  { href: "/threats", label: "Threats", icon: ShieldAlert },
  { href: "/whitelist", label: "Whitelist", icon: ListChecks },
  { href: "/reports", label: "Reports", icon: FileText },
];
const SETTINGS: Item = { href: "/settings", label: "Settings", icon: Settings };

function NavItem({ item, badge }: { item: Item; badge?: number | null }) {
  const pathname = usePathname();
  const params = useSearchParams();
  const { setOpenMobile } = useSidebar();
  const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
  const Icon = item.icon;
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        asChild
        isActive={active}
        className="min-h-10 gap-2.5 rounded-control px-2.5 text-[14px] font-medium text-text-strong no-underline hover:text-text hover:no-underline data-active:text-text"
      >
        <Link
          href={`${item.href}${sharedQuery(params)}`}
          aria-current={active ? "page" : undefined}
          onClick={() => setOpenMobile(false)}
        >
          <Icon aria-hidden="true" />
          <span>{item.label}</span>
          {badge ? (
            <span className="tabular ml-auto inline-flex h-5 min-w-[22px] items-center justify-center rounded-full bg-danger-bg px-[7px] text-xs font-semibold text-danger-fg">
              {badge}
              <span className="sr-only"> live</span>
            </span>
          ) : null}
        </Link>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

export function AppSidebar({ liveThreats }: { liveThreats: number | null }) {
  const { me } = usePanel();
  const demoBrand = me.brands[0]?.name ?? me.customer.name;
  return (
    <Sidebar variant="inset" className="px-2.5 py-3">
      <SidebarHeader className="p-0 pb-5">
        <Logo />
      </SidebarHeader>
      <SidebarContent className="gap-5">
        <nav aria-label="Primary">
          <SidebarMenu className="gap-0.5">
            {PRIMARY.map((item) => (
              <NavItem
                key={item.href}
                item={item}
                badge={item.href === "/threats" ? liveThreats : null}
              />
            ))}
          </SidebarMenu>
        </nav>
        {me.customer.is_demo ? (
          <div className="flex flex-col gap-1 rounded-xl border border-panel-border bg-panel p-3">
            <p className="text-[13px] font-semibold">Demo workspace</p>
            <p className="text-[12.5px] leading-[1.45] text-text-secondary">
              Sample data for {demoBrand}, a fictional brand.
            </p>
          </div>
        ) : null}
      </SidebarContent>
      <SidebarFooter className="p-0">
        <SidebarMenu>
          <NavItem item={SETTINGS} />
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}
