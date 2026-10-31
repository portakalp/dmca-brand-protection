"use client";

import {
  AlertTriangle,
  Check,
  Cloud,
  FileCode,
  FileJson,
  FileText,
  Globe,
  Image as ImageIcon,
  Info,
  Lock,
  Server,
  ShieldCheck,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import { Pill, type PillTone } from "@/components/pill";
import { usePanel } from "@/components/shell/panel";
import { Card, CardTitle, EmptyState } from "@/components/states";
import type { Schemas } from "@/lib/api/client";
import { fileNames, fileSize, retainedUntil, sortFiles } from "@/lib/evidence";
import { categoryLabel, plural } from "@/lib/format";
import { longDay } from "@/lib/period";
import {
  clock,
  dayTime,
  noticeRows,
  noticesCaption,
  ruleNote,
  rulePassed,
  sameDay,
  timelineSteps,
  zoneName,
  type NoticeTone,
  type Step,
} from "@/lib/threat-detail";
import { cn } from "@/lib/utils";

type Detail = Schemas["ThreatDetail"];

function Row({ label, children, last }: { label: string; children: ReactNode; last?: boolean }) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 py-[9px]",
        !last && "border-b border-row-divider",
      )}
    >
      <dt className="text-[13px] text-text-tertiary">{label}</dt>
      <dd className="m-0 text-right text-[13px] font-medium">{children}</dd>
    </div>
  );
}

function Aside({ children }: { children: ReactNode }) {
  return <span className="font-normal text-text-tertiary"> · {children}</span>;
}

// --- AI review -------------------------------------------------------------------------------

const REVIEW_LABELS: Record<Schemas["VerdictSource"], string> = {
  ai_triage: "First review",
  ai_confirm: "Second review",
  staff: "Kalkan team",
};

export function AiReviewCard({ detail }: { detail: Detail }) {
  const { timeZone } = usePanel();
  const reviews = detail.ai_review.reviews;
  const latest = reviews.at(-1);
  const note = ruleNote(detail);
  return (
    <Card
      aria-labelledby="review-title"
      className="flex min-w-0 flex-[3_1_420px] flex-col gap-3.5 px-5 py-[18px]"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <CardTitle id="review-title" title="AI review" />
        {latest ? (
          <p className="text-[12.5px] leading-5 text-text-tertiary">
            Reviewed {dayTime(latest.created_at, timeZone)}
          </p>
        ) : null}
      </div>
      {latest ? (
        <>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <p className="text-[20px] leading-[26px] font-semibold tracking-[-0.015em]">
              {categoryLabel(latest.category)}
            </p>
            <Pill tone="brand" className="px-[9px] text-[12.5px]">
              {latest.confidence.toFixed(2)} confidence
            </Pill>
          </div>
          <p className="text-[14px] leading-[1.55] text-pretty text-text-strong">
            {latest.short_explanation}
          </p>
          <dl className="m-0 flex flex-col border-t border-row-divider">
            {reviews.map((review) => (
              <Row key={review.source} label={REVIEW_LABELS[review.source]}>
                <span className="tabular">
                  {categoryLabel(review.category)} · {review.confidence.toFixed(2)}
                </span>
              </Row>
            ))}
            <Row label="Rule checks">{rulePassed(detail)}</Row>
          </dl>
          {note ? (
            <p className="mt-auto flex items-start gap-2 text-[12.5px] leading-normal text-text-secondary">
              <Info aria-hidden="true" className="mt-px size-4 flex-none text-text-tertiary" />
              <span>{note}</span>
            </p>
          ) : null}
        </>
      ) : (
        <EmptyState title="Not reviewed yet">
          The AI review appears here once the evidence has been checked.
        </EmptyState>
      )}
    </Card>
  );
}

// --- infrastructure --------------------------------------------------------------------------

/** "Cloudflare" for aria.ns.cloudflare.com and jake.ns.cloudflare.com. */
export function nameServers(servers: string[]): string {
  const providers = [
    ...new Set(
      servers.map((server) => {
        const labels = server.toLowerCase().replace(/\.$/, "").split(".");
        const root = labels.slice(-2).join(".");
        return root === "cloudflare.com" ? "Cloudflare" : root;
      }),
    ),
  ];
  return providers.length ? providers.slice(0, 2).join(", ") : "—";
}

function daysBefore(registered: string, seen: string): string {
  const days = Math.floor((Date.parse(seen) - Date.parse(registered)) / 86_400_000);
  if (days <= 0) return "the day of detection";
  return `${days} ${plural(days, "day", "days")} before detection`;
}

function place(name: string, country: string | null): string {
  return country ? `${name}, ${country}` : name;
}

export function InfrastructureCard({ detail }: { detail: Detail }) {
  const infra = detail.infrastructure;
  const tls = infra.tls;
  const rows: [string, ReactNode][] = [
    [
      "Registered",
      infra.registered_at ? (
        <>
          {longDay(infra.registered_at.slice(0, 10))}
          <Aside>{daysBefore(infra.registered_at, detail.threat.first_seen_at)}</Aside>
        </>
      ) : (
        "Unknown"
      ),
    ],
    ["Registrar", infra.registrar ?? "Unknown"],
    ["Name servers", nameServers(infra.nameservers)],
    [
      "Proxy",
      infra.proxy ? (
        <>
          {infra.proxy}
          <Aside>{infra.origin_host ? "origin hidden" : "origin unknown"}</Aside>
        </>
      ) : (
        "None"
      ),
    ],
  ];
  if (infra.proxy && infra.origin_host) {
    rows.push([
      "Origin host",
      <>
        {place(infra.origin_host, infra.origin_country)}
        <Aside>disclosed by {infra.proxy}</Aside>
      </>,
    ]);
  } else if (!infra.proxy) {
    rows.push(["Hosting", infra.hosting ? place(infra.hosting, infra.hosting_country) : "Unknown"]);
  }
  if (tls?.issuer) {
    rows.push([
      "TLS certificate",
      <>
        {tls.issuer}
        {tls.issued_at ? <Aside>issued {tls.issued_at}</Aside> : null}
      </>,
    ]);
  }

  return (
    <Card
      aria-labelledby="infra-title"
      className="flex min-w-0 flex-[2_1_340px] flex-col gap-2.5 px-5 py-[18px]"
    >
      <CardTitle
        id="infra-title"
        title="Infrastructure"
        subtitle="From RDAP, DNS and certificate lookups"
      />
      <dl className="m-0 flex flex-col">
        {rows.map(([label, value], index) => (
          <Row key={label} label={label} last={index === rows.length - 1}>
            {value}
          </Row>
        ))}
      </dl>
    </Card>
  );
}

// --- timeline --------------------------------------------------------------------------------

function StepIcon({ tone }: { tone: Step["tone"] }) {
  if (tone === "current") {
    return (
      <span className="inline-flex size-[22px] flex-none items-center justify-center rounded-full border-2 border-[var(--status-notice)] bg-[var(--status-notice-bg)]">
        <span className="size-2 rounded-full bg-[var(--status-notice)]" />
      </span>
    );
  }
  if (tone === "warning") {
    return (
      <span className="inline-flex size-[22px] flex-none items-center justify-center rounded-full bg-warning-icon-bg text-warning-icon">
        <AlertTriangle className="size-3" strokeWidth={2.5} />
      </span>
    );
  }
  return (
    <span className="inline-flex size-[22px] flex-none items-center justify-center rounded-full bg-brand text-white">
      <Check className="size-3" strokeWidth={3} />
    </span>
  );
}

export function TimelineCard({ detail }: { detail: Detail }) {
  const { timeZone } = usePanel();
  const steps = timelineSteps(detail, timeZone);
  const moments = steps.flatMap((step) => (step.at ? [step.at] : []));
  const oneDay = moments.length > 0 && sameDay(moments, timeZone);
  const zone = zoneName(timeZone, moments[0] ?? new Date().toISOString());
  const caption = oneDay
    ? `${dayTime(moments[0], timeZone).split(",")[0]} · times in ${zone}`
    : `Times in ${zone}`;

  return (
    <Card
      aria-labelledby="timeline-title"
      className="flex min-w-0 flex-[3_1_420px] flex-col gap-4 px-5 pt-[18px] pb-1.5"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <CardTitle id="timeline-title" title="Timeline" />
        <p className="text-[12.5px] leading-5 text-text-tertiary">{caption}</p>
      </div>
      <ol className="m-0 flex list-none flex-col p-0">
        {steps.map((step, index) => (
          <li key={step.id} className="flex gap-3.5">
            <div aria-hidden="true" className="flex w-[22px] flex-none flex-col items-center">
              <StepIcon tone={step.tone} />
              {index < steps.length - 1 ? (
                <span className="my-1 w-0.5 flex-auto rounded-sm bg-control-border" />
              ) : null}
            </div>
            <div className="flex min-w-0 flex-auto flex-col gap-0.5 pt-px pb-[18px]">
              <div className="flex flex-wrap justify-between gap-x-3 gap-y-0.5">
                <span className="text-[14px] leading-5 font-semibold">{step.title}</span>
                {step.at ? (
                  <time
                    dateTime={step.at}
                    className="tabular text-[13px] leading-5 text-text-secondary"
                  >
                    {oneDay ? clock(step.at, timeZone) : dayTime(step.at, timeZone)}
                  </time>
                ) : (
                  <span className="text-[13px] leading-5 font-medium text-[var(--status-notice-fg)]">
                    Now
                  </span>
                )}
              </div>
              <p className="text-[13px] leading-normal text-pretty text-text-secondary">
                {step.text}
              </p>
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}

// --- notices ---------------------------------------------------------------------------------

const CHANNEL_ICONS: Record<Schemas["Channel"], LucideIcon> = {
  cloudflare: Cloud,
  hosting: Server,
  registrar: Globe,
  registry: Globe,
  google: ShieldCheck,
};

const NOTICE_TONES: Record<NoticeTone, PillTone> = {
  neutral: "neutral",
  waiting: "waiting",
  success: "success",
  danger: "danger",
  scheduled: "scheduled",
};

export function NoticesCard({ detail }: { detail: Detail }) {
  const { timeZone } = usePanel();
  const rows = noticeRows(detail, timeZone);
  return (
    <Card aria-labelledby="notices-title" className="flex flex-col gap-2 px-5 pt-[18px] pb-2.5">
      <CardTitle id="notices-title" title="Notices" subtitle={noticesCaption(rows, detail)} />
      {rows.length ? (
        <ul className="m-0 flex list-none flex-col p-0">
          {rows.map((row) => {
            const Icon = row.channel ? CHANNEL_ICONS[row.channel] : UserRound;
            return (
              <li
                key={row.id}
                className="flex items-center gap-3 border-row-divider py-2.5 not-last:border-b"
              >
                <span
                  aria-hidden="true"
                  className="inline-flex size-8 flex-none items-center justify-center rounded-[8px] bg-segment text-text-secondary"
                >
                  <Icon className="size-4" />
                </span>
                <span className="flex min-w-0 flex-auto flex-col">
                  <span className="text-[13.5px] font-medium">{row.title}</span>
                  <span className="text-xs text-text-tertiary">{row.subtitle}</span>
                </span>
                <Pill tone={NOTICE_TONES[row.tone]}>{row.status}</Pill>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="pb-2 text-[13px] text-text-secondary">
          Notices appear here once the threat is confirmed.
        </p>
      )}
    </Card>
  );
}

// --- evidence --------------------------------------------------------------------------------

const FILE_ICONS: Record<Schemas["ArtifactKind"], LucideIcon> = {
  screenshot: ImageIcon,
  screenshot_full: ImageIcon,
  html: FileCode,
  har: FileText,
  rdap: FileJson,
  dns: FileJson,
  tls: FileJson,
  favicon: ImageIcon,
};

export function EvidenceCard({ detail }: { detail: Detail }) {
  const files = sortFiles(detail.files);
  const names = fileNames(files);
  const until = retainedUntil(files);
  const total = files.reduce((sum, file) => sum + file.bytes, 0);
  return (
    <Card
      aria-labelledby="evidence-title"
      className="flex flex-auto flex-col gap-2 px-5 pt-[18px] pb-2.5"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <div>
          <h2 id="evidence-title" className="text-[14px] leading-5 font-semibold">
            Evidence package
          </h2>
          {until ? (
            <p className="mt-0.5 flex items-center gap-1.5 text-[12.5px] leading-[18px] text-text-tertiary">
              <Lock aria-hidden="true" className="size-[13px]" />
              Stored unchanged until {longDay(until.slice(0, 10))}
            </p>
          ) : null}
        </div>
        {files.length ? (
          <p className="tabular text-[12.5px] leading-5 text-text-tertiary">
            {files.length} {plural(files.length, "file", "files")} · {fileSize(total)}
          </p>
        ) : null}
      </div>
      {files.length ? (
        <ul className="m-0 flex list-none flex-col p-0">
          {files.map((file) => {
            const Icon = FILE_ICONS[file.kind];
            const name = names.get(file.id)!;
            return (
              <li key={file.id} className="border-row-divider not-last:border-b">
                <a
                  href={file.url}
                  download
                  aria-label={`Download ${name}, ${fileSize(file.bytes)}`}
                  className="flex min-h-10 items-center gap-2.5 text-text no-underline hover:text-text hover:no-underline"
                >
                  <Icon aria-hidden="true" className="size-4 flex-none text-text-tertiary" />
                  <span className="min-w-0 flex-auto truncate font-mono text-[12.5px]">{name}</span>
                  <span className="tabular text-[12.5px] text-text-tertiary">
                    {fileSize(file.bytes)}
                  </span>
                </a>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="pb-2 text-[13px] text-text-secondary">No evidence has been captured yet.</p>
      )}
    </Card>
  );
}
