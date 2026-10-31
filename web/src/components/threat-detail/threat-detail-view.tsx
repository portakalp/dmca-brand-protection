"use client";

import { ChevronRight, Download } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { Pill } from "@/components/pill";
import { BrandPicker } from "@/components/shell/brand-picker";
import { Notifications } from "@/components/shell/notifications";
import { usePanel } from "@/components/shell/panel";
import { UserMenu } from "@/components/shell/user-menu";
import { Card, ErrorState } from "@/components/states";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { accessOf } from "@/lib/access";
import type { Schemas } from "@/lib/api/client";
import { useThreat } from "@/lib/api/queries";
import { categoryLabel } from "@/lib/format";
import { fullMoment, sourceLabel } from "@/lib/threat-detail";
import { sharedQuery } from "@/lib/url";

import { AiReviewCard, EvidenceCard, InfrastructureCard, NoticesCard, TimelineCard } from "./cards";
import { Comparison } from "./comparison";
import { MarkOurs } from "./mark-ours";

function TopBar({ domain }: { domain: string | null }) {
  const params = useSearchParams();
  const query = sharedQuery(params);
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3">
      <nav aria-label="Breadcrumb" className="min-w-0">
        <ol className="m-0 flex list-none flex-wrap items-center gap-1 p-0 text-[13.5px]">
          {[
            { href: `/overview${query}`, label: "Overview" },
            { href: `/threats${query}`, label: "Threats" },
          ].map((crumb) => (
            <li key={crumb.label} className="flex items-center gap-1">
              <Link
                href={crumb.href}
                className="inline-flex min-h-8 items-center px-1 font-medium text-text-secondary no-underline hover:text-text"
              >
                {crumb.label}
              </Link>
              <ChevronRight aria-hidden="true" className="size-3.5 text-[var(--status-watching)]" />
            </li>
          ))}
          <li className="flex min-w-0 items-center">
            <span
              aria-current="page"
              className="truncate px-1 font-mono text-[12.5px] font-medium text-text"
            >
              {domain ?? "Threat"}
            </span>
          </li>
        </ol>
      </nav>
      <div className="flex flex-wrap items-center gap-2">
        <BrandPicker />
        <Notifications />
        <UserMenu />
      </div>
    </div>
  );
}

function Header({ detail }: { detail: Schemas["ThreatDetail"] }) {
  const { me, timeZone } = usePanel();
  const access = accessOf(me);
  const threat = detail.threat;
  const source = sourceLabel(detail.source);
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div className="flex min-w-0 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <h1 className="font-mono text-[24px] leading-[30px] font-semibold tracking-[-0.02em] [overflow-wrap:anywhere]">
            {threat.domain}
          </h1>
          <StatusBadge status={threat.status} className="pr-2.5 pl-2 text-[12.5px]" />
          <Pill className="px-2.5 text-[12.5px]">{categoryLabel(threat.category)}</Pill>
        </div>
        <p className="text-[13px] text-text-tertiary">
          First seen {fullMoment(threat.first_seen_at, timeZone)}
          {source ? ` · ${source}` : null}
          {detail.official_domain ? (
            <>
              {" · Impersonates "}
              <span className="font-mono text-[12.5px] text-text-strong">
                {detail.official_domain}
              </span>
            </>
          ) : null}
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <MarkOurs detail={detail} mode={access.markOurs} />
        <Button asChild size="control" className="min-h-10 pr-3.5 pl-3 font-semibold">
          <a
            href={detail.evidence_zip_url}
            download
            className="text-primary-foreground no-underline hover:text-primary-foreground hover:no-underline"
          >
            <Download aria-hidden="true" />
            Download evidence
          </a>
        </Button>
      </div>
    </header>
  );
}

function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading threat" className="flex flex-col gap-5">
      <Skeleton className="h-16 rounded-card" />
      <Skeleton className="h-[420px] rounded-card" />
      <div className="flex flex-wrap gap-4">
        <Skeleton className="h-72 flex-[3_1_420px] rounded-card" />
        <Skeleton className="h-72 flex-[2_1_340px] rounded-card" />
      </div>
    </div>
  );
}

export function ThreatDetailView({ id }: { id: string }) {
  const threat = useThreat(id);
  return (
    <>
      <TopBar domain={threat.data?.threat.domain ?? null} />
      {threat.isPending ? (
        <Loading />
      ) : threat.isError ? (
        <Card>
          <ErrorState error={threat.error} onRetry={() => threat.refetch()} />
        </Card>
      ) : (
        <>
          <Header detail={threat.data} />
          <Comparison detail={threat.data} />
          <div className="flex flex-wrap items-stretch gap-4">
            <AiReviewCard detail={threat.data} />
            <InfrastructureCard detail={threat.data} />
          </div>
          <div className="flex flex-wrap items-stretch gap-4">
            <TimelineCard detail={threat.data} />
            <div className="flex min-w-0 flex-[2_1_340px] flex-col gap-4">
              <NoticesCard detail={threat.data} />
              <EvidenceCard detail={threat.data} />
            </div>
          </div>
        </>
      )}
    </>
  );
}
