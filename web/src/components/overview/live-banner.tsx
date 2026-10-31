"use client";

import { ArrowRight, ShieldCheck, TriangleAlert } from "lucide-react";
import Link from "next/link";

import { usePanel } from "@/components/shell/panel";
import { Skeleton } from "@/components/ui/skeleton";
import { useThreatCount } from "@/lib/api/queries";
import { liveSentence, plural } from "@/lib/format";
import { withParams } from "@/lib/url";

export function LiveBanner({ live }: { live: number }) {
  const { brand } = usePanel();
  const brandId = brand?.id ?? null;
  const notified = useThreatCount(brandId, "notice_sent", live > 0);
  const active = useThreatCount(brandId, "active", live > 0);

  if (live === 0) {
    return (
      <section
        aria-label="Live threats"
        className="flex items-center gap-3.5 rounded-xl border border-card-border bg-card py-3 pr-3 pl-3.5"
      >
        <span
          aria-hidden="true"
          className="inline-flex size-8 flex-none items-center justify-center rounded-lg bg-success-bg text-success-fg"
        >
          <ShieldCheck className="size-4" />
        </span>
        <p className="text-[14px] text-text-secondary">
          <strong className="font-semibold text-text">No threats are live.</strong> Everything we
          found is taken down, watched or dismissed.
        </p>
      </section>
    );
  }

  const status = (notified.data ?? 0) >= (active.data ?? 0) ? "notice_sent" : "active";
  const href = `/threats${withParams("", { brand: brandId, status })}`;
  return (
    <section
      aria-label="Live threats"
      className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 rounded-xl border border-warning-border bg-warning-bg py-3 pr-3 pl-3.5"
    >
      <span
        aria-hidden="true"
        className="inline-flex size-8 flex-none items-center justify-center rounded-lg bg-warning-icon-bg text-warning-icon"
      >
        <TriangleAlert className="size-4" />
      </span>
      <p className="min-w-0 flex-[1_1_320px] text-[14px] text-warning-fg">
        <strong className="font-semibold">
          {live} {plural(live, "threat is", "threats are")} still live.
        </strong>{" "}
        {notified.data !== undefined && active.data !== undefined ? (
          liveSentence(notified.data, active.data)
        ) : (
          <Skeleton className="inline-block h-3.5 w-56 align-middle" />
        )}
      </p>
      <Link
        href={href}
        className="inline-flex min-h-10 items-center gap-1.5 rounded-control bg-warning-icon-bg px-3 text-[13.5px] font-semibold text-warning-fg no-underline hover:text-warning-fg hover:no-underline"
      >
        Review live threats
        <ArrowRight aria-hidden="true" className="size-4" />
      </Link>
    </section>
  );
}
