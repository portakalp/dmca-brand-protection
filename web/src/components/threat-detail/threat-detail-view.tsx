"use client";

import { ChevronRight, Download } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import type { ReactNode } from "react";

import { usePanel } from "@/components/shell/panel";
import { Card, ErrorState, LoadingState } from "@/components/states";
import { Status } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { accessOf } from "@/lib/access";
import type { Schemas } from "@/lib/api/client";
import { useThreat } from "@/lib/api/queries";
import { categoryLabel } from "@/lib/format";
import { clock, dayTime, fullMoment, sameDay, siteCheck, sourceFact } from "@/lib/threat-detail";
import { sharedQuery } from "@/lib/url";

import { AiReviewCard, EvidenceCard, InfrastructureCard, NoticesCard, TimelineCard } from "./cards";
import { Comparison } from "./comparison";
import { MarkOurs } from "./mark-ours";
import { TakedownProgress } from "./progress";

function Breadcrumb({ number }: { number: string | null }) {
  const params = useSearchParams();
  return (
    <nav aria-label="Breadcrumb">
      <ol className="m-0 flex list-none flex-wrap items-center gap-2 p-0 text-[13px] text-text-3">
        <li className="flex items-center gap-2">
          <Link
            href={`/threats${sharedQuery(params)}`}
            className="text-text-2 no-underline hover:text-text hover:underline"
          >
            Threats
          </Link>
          <ChevronRight aria-hidden="true" strokeWidth={1.5} className="size-3.5" />
        </li>
        <li>
          <span aria-current="page" className="font-mono text-[12.5px]">
            {number ?? "Threat"}
          </span>
        </li>
      </ol>
    </nav>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span>
      <span className="text-text-3">{label}</span> {children}
    </span>
  );
}

function Header({ detail }: { detail: Schemas["ThreatDetail"] }) {
  const { me, timeZone } = usePanel();
  const access = accessOf(me);
  const threat = detail.threat;
  const source = sourceFact(detail.source);
  const check = siteCheck(detail);
  const now = new Date().toISOString();
  const at = (moment: string) =>
    sameDay([moment, now], timeZone) ? clock(moment, timeZone) : dayTime(moment, timeZone);
  const help =
    access.markOurs === "disabled"
      ? me.customer.is_demo
        ? "Changes are turned off in this demo workspace."
        : "Changes are turned off while this account is read-only."
      : access.markOurs === "enabled"
        ? "Only workspace owners can mark a domain as yours."
        : null;

  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
      <div className="min-w-0">
        <p className="mb-1.5 flex flex-wrap gap-2 text-[13.5px] text-text-2">
          <span className="font-mono">{threat.number}</span>
          <span aria-hidden="true">·</span>
          <span>{categoryLabel(threat.category)}</span>
        </p>
        <h1 className="font-mono text-[28px] font-semibold tracking-[-0.01em] [overflow-wrap:anywhere] max-sm:text-[22px]">
          {threat.domain}
        </h1>
        <div className="mt-2.5 flex flex-wrap items-center gap-x-[22px] gap-y-1.5 text-[13.5px] text-text-2">
          <Status status={threat.status} />
          <Fact label="First seen">{fullMoment(threat.first_seen_at, timeZone)}</Fact>
          {check?.lastAt ? (
            <Fact label="Last checked">
              {at(check.lastAt)}, {check.online ? "still online" : "offline"}
            </Fact>
          ) : null}
          {source ? <Fact label={source.label}>{source.value}</Fact> : null}
          {detail.official_domain ? (
            <Fact label="Imitates">
              <span className="font-mono text-text">{detail.official_domain}</span>
            </Fact>
          ) : null}
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap gap-2">
          <MarkOurs detail={detail} mode={access.markOurs} helpId="mark-ours-help" />
          <Button asChild>
            <a
              href={detail.evidence_zip_url}
              download
              className="text-on-accent hover:text-on-accent"
            >
              <Download aria-hidden="true" />
              Download evidence
            </a>
          </Button>
        </div>
        {help ? (
          <p id="mark-ours-help" className="text-[12px] text-text-3">
            {help}
          </p>
        ) : null}
      </div>
    </header>
  );
}

function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading threat" className="flex flex-col gap-5">
      <Skeleton className="h-24" />
      <Card>
        <LoadingState rows={3} />
      </Card>
      <Card>
        <LoadingState rows={6} />
      </Card>
    </div>
  );
}

export function ThreatDetailView({ id }: { id: string }) {
  const threat = useThreat(id);
  return (
    <>
      <Breadcrumb number={threat.data?.threat.number ?? null} />
      {threat.isPending ? (
        <Loading />
      ) : threat.isError ? (
        <Card>
          <ErrorState error={threat.error} onRetry={() => threat.refetch()} />
        </Card>
      ) : (
        <>
          <Header detail={threat.data} />
          <TakedownProgress detail={threat.data} />
          <Comparison detail={threat.data} />
          <div className="grid grid-cols-1 items-stretch gap-5 min-[1181px]:grid-cols-2">
            <AiReviewCard detail={threat.data} />
            <div className="flex min-w-0 flex-col gap-5">
              <InfrastructureCard detail={threat.data} />
              <NoticesCard detail={threat.data} />
            </div>
          </div>
          <div className="grid grid-cols-1 items-stretch gap-5 min-[1181px]:grid-cols-2">
            <TimelineCard detail={threat.data} />
            <EvidenceCard detail={threat.data} />
          </div>
        </>
      )}
    </>
  );
}
