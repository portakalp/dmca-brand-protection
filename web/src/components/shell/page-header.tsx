"use client";

import { Download } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import type { Range, RangeKey } from "@/lib/period";

import { BrandPicker } from "./brand-picker";
import { DateRangePicker } from "./date-range-picker";
import { Notifications } from "./notifications";
import { UserMenu } from "./user-menu";

/** The title row of every page: title and subtitle, then brand, period, bell and account. */
export function PageHeader({
  title,
  subtitle,
  range,
  exportReport = false,
}: {
  title: string;
  subtitle?: ReactNode;
  range?: { value: Range; fallback: RangeKey };
  exportReport?: boolean;
}) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3">
      <div className="flex min-w-0 flex-col gap-0.5">
        <h1 className="text-[22px] leading-7 font-semibold tracking-[-0.015em]">{title}</h1>
        {subtitle ? <p className="text-[13px] text-text-tertiary">{subtitle}</p> : null}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <BrandPicker />
        {range ? <DateRangePicker range={range.value} fallback={range.fallback} /> : null}
        {exportReport ? (
          <Button asChild variant="control" size="control" className="pr-3.5">
            <Link
              href="/reports"
              className="text-text no-underline hover:text-text hover:no-underline"
            >
              <Download aria-hidden="true" className="text-text-secondary" />
              Export report
            </Link>
          </Button>
        ) : null}
        <Notifications />
        <UserMenu />
      </div>
    </header>
  );
}
