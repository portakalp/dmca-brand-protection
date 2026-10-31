"use client";

import { Copy, Download, Lock, Undo2, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { toast } from "sonner";

import { usePanel } from "@/components/shell/panel";
import { Card, EmptyState, Note, PanelBody, PanelHeader } from "@/components/states";
import { Indicator } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import {
  fileLabel,
  fileNames,
  fileSize,
  retainedUntil,
  shortHash,
  sortFiles,
} from "@/lib/evidence";
import { categoryLabel, plural } from "@/lib/format";
import { longDay } from "@/lib/period";
import {
  clock,
  countryName,
  dayTime,
  noticeRows,
  noticesCaption,
  percent,
  ruleNote,
  ruleOutcome,
  sameDay,
  timelineSteps,
  zoneName,
  type NoticeRow,
} from "@/lib/threat-detail";
import { cn } from "@/lib/utils";

type Detail = Schemas["ThreatDetail"];

/** A label/value list: labels in a fixed column, rows split by dividers. */
function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="m-0 grid grid-cols-[140px_minmax(0,1fr)] max-sm:grid-cols-[110px_minmax(0,1fr)]">
      {rows.map(([label, value], index) => (
        <div key={label} className="contents">
          <dt
            className={cn(
              "py-2.5 pr-3 text-[13px] text-text-2",
              index > 0 && "border-t border-border",
            )}
          >
            {label}
          </dt>
          <dd className={cn("m-0 py-2.5 font-medium", index > 0 && "border-t border-border")}>
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Aside({ children }: { children: ReactNode }) {
  return <span className="ml-1.5 text-[12.5px] font-normal text-text-3">{children}</span>;
}

// --- automated review ------------------------------------------------------------------------

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
    <Card aria-labelledby="review-title" className="flex flex-col">
      <PanelHeader
        id="review-title"
        title="Automated review"
        description="Two AI reviews and our rule checks decide whether a site is a threat."
        actions={
          latest ? (
            <span className="text-[13px] text-text-2 tabular">
              {dayTime(latest.created_at, timeZone)}
            </span>
          ) : null
        }
      />
      {latest ? (
        <PanelBody className="flex flex-1 flex-col">
          <p className="mb-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-display text-[20px] font-semibold">
              {categoryLabel(latest.category)}
            </span>
            <span className="text-text-2">{percent(latest.confidence)} confidence</span>
          </p>
          <p className="mb-3 text-[12.5px] text-text-3">
            Confidence is how sure the review is of this verdict. It is separate from visual
            similarity, which only compares screenshots.
          </p>
          <p className="mb-3.5 text-pretty">{latest.short_explanation}</p>
          <Facts
            rows={[
              ...reviews.map((review): [string, ReactNode] => [
                REVIEW_LABELS[review.source],
                <>
                  {categoryLabel(review.category)}
                  <Aside>at {percent(review.confidence)} confidence</Aside>
                </>,
              ]),
              ["Rule checks", ruleOutcome(detail)],
            ]}
          />
          {note ? <Note className="mt-3.5">{note}</Note> : null}
        </PanelBody>
      ) : (
        <EmptyState title="Not reviewed yet">
          The automated review appears here once the evidence has been checked.
        </EmptyState>
      )}
    </Card>
  );
}

// --- who runs the site -----------------------------------------------------------------------

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
  return country ? `${name}, ${countryName(country)}` : name;
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
          <Aside>hides the real host</Aside>
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
  } else if (infra.proxy) {
    rows.push(["Origin host", "Not disclosed yet"]);
  } else {
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
    <Card aria-labelledby="infra-title" className="flex flex-col">
      <PanelHeader
        id="infra-title"
        title="Who runs the site"
        description="From domain registration records (RDAP), DNS and the site's certificate."
      />
      <PanelBody>
        <Facts rows={rows} />
      </PanelBody>
    </Card>
  );
}

// --- abuse notices ---------------------------------------------------------------------------

function noticeIcon(row: NoticeRow): LucideIcon | undefined {
  return row.status === "Withdrawn" ? Undo2 : undefined;
}

export function NoticesCard({ detail }: { detail: Detail }) {
  const { timeZone } = usePanel();
  const rows = noticeRows(detail, timeZone);
  return (
    <Card aria-labelledby="notices-title" className="flex flex-1 flex-col">
      <PanelHeader
        id="notices-title"
        title="Abuse notices"
        description={noticesCaption(rows, detail)}
      />
      <PanelBody>
        {rows.length ? (
          <ul className="m-0 flex list-none flex-col p-0">
            {rows.map((row, index) => (
              <li
                key={row.id}
                className={cn(
                  // The status keeps its column; a long subtitle wraps instead.
                  "flex items-start justify-between gap-x-3",
                  index > 0 ? "border-t border-border py-[11px]" : "pb-[11px]",
                  index === rows.length - 1 && "pb-0",
                )}
              >
                <div className="min-w-0 flex-1">
                  <p className="font-semibold">{row.title}</p>
                  <p className="text-[12.5px] text-text-3 tabular">{row.subtitle}</p>
                </div>
                <Indicator
                  tone={row.tone}
                  icon={noticeIcon(row)}
                  className="flex-none text-[13.5px]"
                >
                  {row.status}
                </Indicator>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[13px] text-text-2">
            Notices appear here once the threat is confirmed.
          </p>
        )}
      </PanelBody>
    </Card>
  );
}

// --- timeline --------------------------------------------------------------------------------

export function TimelineCard({ detail }: { detail: Detail }) {
  const { timeZone } = usePanel();
  const steps = timelineSteps(detail, timeZone);
  const moments = steps.flatMap((step) => (step.at ? [step.at] : []));
  const oneDay = moments.length > 0 && sameDay(moments, timeZone);
  const zone = zoneName(timeZone, moments[0] ?? new Date().toISOString());
  const description = oneDay
    ? `Everything that happened on this case, ${dayTime(moments[0], timeZone).split(",")[0]}, times in ${zone}.`
    : `Everything that happened on this case, times in ${zone}.`;

  return (
    <Card aria-labelledby="timeline-title" className="flex flex-col">
      <PanelHeader id="timeline-title" title="Timeline" description={description} />
      <PanelBody>
        <ol className="m-0 list-none p-0">
          {steps.map((step, index) => {
            const last = index === steps.length - 1;
            const current = step.tone === "current";
            return (
              <li
                key={step.id}
                className={cn(
                  "relative grid gap-x-2.5",
                  oneDay
                    ? "grid-cols-[52px_20px_minmax(0,1fr)]"
                    : "grid-cols-[64px_20px_minmax(0,1fr)]",
                  !last && "pb-4",
                )}
              >
                {step.at ? (
                  <time
                    dateTime={step.at}
                    className="pt-px text-right text-[13px] leading-[1.35] text-text-2 tabular"
                  >
                    {oneDay ? (
                      clock(step.at, timeZone)
                    ) : (
                      <>
                        <span className="block text-[12px] text-text-3">
                          {dayTime(step.at, timeZone).split(",")[0]}
                        </span>
                        {clock(step.at, timeZone)}
                      </>
                    )}
                  </time>
                ) : (
                  <span className="pt-px text-right text-[13px] text-text-2">Now</span>
                )}
                <span aria-hidden="true" className="relative flex justify-center">
                  {!last ? (
                    <span className="absolute top-1.5 -bottom-[22px] w-0.5 bg-border-strong" />
                  ) : null}
                  <span
                    className={cn(
                      "relative mt-[5px] size-2.5 rounded-full shadow-[0_0_0_3px_var(--surface)]",
                      current
                        ? "border-2 border-st-warning bg-surface"
                        : step.tone === "warning"
                          ? "bg-st-warning"
                          : "bg-text-2",
                    )}
                  />
                </span>
                <div className="min-w-0">
                  <p className="font-semibold">{step.title}</p>
                  <p className="text-[13px] text-pretty text-text-2">{step.text}</p>
                </div>
              </li>
            );
          })}
        </ol>
      </PanelBody>
    </Card>
  );
}

// --- evidence package ------------------------------------------------------------------------

async function copyHash(name: string, hash: string) {
  try {
    await navigator.clipboard.writeText(hash);
    toast.success(`SHA-256 for ${name} copied`);
  } catch {
    toast.error("Couldn't copy. Select the hash and copy it instead.");
  }
}

export function EvidenceCard({ detail }: { detail: Detail }) {
  const files = sortFiles(detail.files);
  const names = fileNames(files);
  const until = retainedUntil(files);
  const total = files.reduce((sum, file) => sum + file.bytes, 0);
  return (
    <Card aria-labelledby="evidence-title" className="flex flex-col">
      <PanelHeader
        id="evidence-title"
        title="Evidence package"
        description={
          until ? (
            <>
              <Lock
                aria-hidden="true"
                strokeWidth={1.5}
                className="mr-1 inline size-[13px] align-[-2px]"
              />
              Stored unchanged until {longDay(until.slice(0, 10))}. Each file&apos;s SHA-256
              fingerprint proves it was not altered.
            </>
          ) : (
            "Each file's SHA-256 fingerprint proves it was not altered."
          )
        }
        actions={
          files.length ? (
            <span className="text-[13px] text-text-2 tabular">
              {files.length} {plural(files.length, "file", "files")} · {fileSize(total)}
            </span>
          ) : null
        }
      />
      <PanelBody>
        {files.length ? (
          <ul className="m-0 list-none p-0">
            {files.map((file, index) => {
              const name = names.get(file.id)!;
              return (
                <li
                  key={file.id}
                  className={cn(
                    "grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-x-3 gap-y-1",
                    index > 0 ? "border-t border-border py-2.5" : "pb-2.5",
                    index === files.length - 1 && "pb-0",
                  )}
                >
                  <span className="min-w-0 truncate">
                    <span className="font-mono text-[13px] font-medium">{name}</span>
                    <span className="ml-1.5 text-[12.5px] text-text-3">{fileLabel(file)}</span>
                  </span>
                  <span className="text-[13px] text-text-2 tabular">{fileSize(file.bytes)}</span>
                  <Button asChild variant="outline" size="icon-sm">
                    <a
                      href={file.url}
                      download
                      aria-label={`Download ${name}, ${fileSize(file.bytes)}`}
                      title={`Download ${name}`}
                    >
                      <Download aria-hidden="true" />
                    </a>
                  </Button>
                  <span className="col-span-full flex min-w-0 items-center gap-1.5 text-[12px] text-text-3">
                    SHA-256
                    <span className="min-w-0 truncate font-mono text-text-2" title={file.sha256}>
                      {shortHash(file.sha256)}
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Copy SHA-256 for ${name}`}
                      title="Copy full SHA-256"
                      onClick={() => void copyHash(name, file.sha256)}
                    >
                      <Copy aria-hidden="true" />
                    </Button>
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-[13px] text-text-2">No evidence has been captured yet.</p>
        )}
      </PanelBody>
    </Card>
  );
}
