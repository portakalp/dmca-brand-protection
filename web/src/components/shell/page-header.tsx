"use client";

import { Download } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import type { Range, RangeKey } from "@/lib/period";
import { cn } from "@/lib/utils";

import { PeriodControl } from "./date-range-picker";

/**
 * The title row of every page: h1 and one lede sentence (scope, period, time zone) on the left;
 * the page's one filter (Period), its actions and "Export report" on the right; a divider below.
 * Brand, account and theme live in the sidebar.
 */
export function PageHeader({
  title,
  subtitle,
  range,
  exportReport = false,
  actions,
  className,
}: {
  title: ReactNode;
  /** The lede: one plain sentence under the title. */
  subtitle?: ReactNode;
  range?: { value: Range; fallback: RangeKey };
  exportReport?: boolean;
  /** Extra buttons, placed before Export report. */
  actions?: ReactNode;
  className?: string;
}) {
  const hasTools = !!range || exportReport || !!actions;
  return (
    <header
      className={cn(
        "flex flex-wrap items-end justify-between gap-x-6 gap-y-4 border-b border-border pb-5",
        className,
      )}
    >
      <div className="flex min-w-0 flex-[1_1_320px] flex-col gap-1.5">
        <h1 className="max-md:text-[24px]">{title}</h1>
        {subtitle ? <p className="text-text-2">{subtitle}</p> : null}
      </div>
      {hasTools ? (
        <div className="flex flex-wrap items-center gap-2 max-sm:w-full">
          {range ? <PeriodControl range={range.value} fallback={range.fallback} /> : null}
          {actions}
          {exportReport ? (
            <Button asChild variant="outline">
              <Link href="/reports">
                <Download aria-hidden="true" />
                Export report
              </Link>
            </Button>
          ) : null}
        </div>
      ) : null}
    </header>
  );
}
