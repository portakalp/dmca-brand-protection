"use client";

import { OctagonAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { usePanel } from "@/components/shell/panel";
import { Card, Hours, PanelBody, PanelHeader } from "@/components/states";
import { Indicator } from "@/components/status-badge";
import type { Schemas } from "@/lib/api/client";
import {
  CHECK_INTERVAL_HOURS,
  clock,
  dayTime,
  sameDay,
  siteCheck,
  takedownProgress,
  type Progress,
  type SiteCheck,
} from "@/lib/threat-detail";
import { cn } from "@/lib/utils";

type Detail = Schemas["ThreatDetail"];

/** The current time, moved on once a minute so the clock keeps up while the page is open. */
export function useNow(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** "09:40" on the same day as now, "Nov 2, 10:05" otherwise. */
function moment(at: string, now: Date, timeZone: string): string {
  return sameDay([at, now.toISOString()], timeZone) ? clock(at, timeZone) : dayTime(at, timeZone);
}

function spoken(hours: number): string {
  const total = Math.round(hours * 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  const parts = [];
  if (h) parts.push(`${h} ${h === 1 ? "hour" : "hours"}`);
  if (m || !h) parts.push(`${m} ${m === 1 ? "minute" : "minutes"}`);
  return parts.join(" ");
}

/** The width of an element, kept current as it resizes; 0 before the first layout. */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

// Room the end labels take ("Reported 09:40", "Registry escalation Nov 2, 10:05") and the Now label.
const START_LABEL = 84;
const END_LABEL = 140;
const NOW_LABEL = 56;

function Track({
  progress,
  now,
  timeZone,
}: {
  progress: Extract<Progress, { kind: "open" }>;
  now: Date;
  timeZone: string;
}) {
  const escalation = progress.escalation!;
  const position = (progress.position ?? 0) * 100;
  const reported = moment(progress.reportedAt, now, timeZone);
  const nowLabel = moment(now.toISOString(), now, timeZone);
  const due = dayTime(escalation.at, timeZone);
  const [ref, width] = useWidth<HTMLDivElement>();
  // Now shares the label row when it clears both end labels; otherwise it drops to a second row.
  const nowLeft = (position / 100) * (width - NOW_LABEL);
  const inline = width > 0 && nowLeft > START_LABEL && nowLeft + NOW_LABEL < width - END_LABEL;
  const label =
    `Reported at ${reported}. Now ${nowLabel}, ${spoken(progress.elapsed)} later. ` +
    `${escalation.label} on ${due}.`;
  return (
    <div
      ref={ref}
      role="img"
      aria-label={label}
      className={cn("relative min-w-0", inline ? "h-[62px]" : "h-[92px]")}
    >
      <span className="absolute inset-x-0 top-3 h-1.5 rounded-[3px] bg-grid" />
      <span
        className="absolute top-3 left-0 h-1.5 rounded-l-[3px] bg-takedown"
        style={{ width: `${position}%` }}
      />
      <span className="absolute top-1.5 left-0 h-[18px] border-l-2 border-ref" />
      <span
        className="absolute top-1.5 h-[18px] border-l-2 border-ref"
        style={{ left: `calc(${position}% - 1px)` }}
      />
      <span className="absolute top-1.5 right-0 h-[18px] border-l-2 border-st-critical" />
      <span className="absolute top-[30px] left-0 text-[12px] leading-[1.35] whitespace-nowrap text-text-2 tabular">
        <b className="block font-semibold text-text">Reported</b>
        {reported}
      </span>
      <span className="absolute top-[30px] right-0 text-right text-[12px] leading-[1.35] whitespace-nowrap text-text-2 tabular">
        <b className="block font-semibold text-text">{escalation.label}</b>
        {due}
      </span>
      {/* Now is held inside the track whatever its position. */}
      <span
        className={cn(
          "absolute text-[12px] leading-[1.35] whitespace-nowrap text-text-2 tabular",
          inline ? "top-[30px]" : "top-[62px]",
        )}
        style={{ left: `${position}%`, transform: `translateX(-${position}%)` }}
      >
        <b className={cn("font-semibold text-text", inline && "block")}>Now</b>
        {inline ? null : " "}
        {nowLabel}
      </span>
    </div>
  );
}

function Checks({ check, now, timeZone }: { check: SiteCheck; now: Date; timeZone: string }) {
  return (
    <div className="flex flex-col gap-2.5 text-[13px]">
      <div className="flex flex-col gap-px">
        <span className="text-text-2">
          {check.lastAt ? `Last check ${moment(check.lastAt, now, timeZone)}` : "Checks so far"}
        </span>
        {check.online ? (
          <Indicator tone="critical" icon={OctagonAlert} className="text-[14px] font-semibold">
            Still online
          </Indicator>
        ) : (
          <Indicator tone="good" className="text-[14px] font-semibold">
            Offline since {moment(check.offlineSince!, now, timeZone)}
          </Indicator>
        )}
      </div>
      {check.nextAt ? (
        <div className="flex flex-col gap-px">
          <span className="text-text-2">Next check</span>
          <span className="text-[14px]">
            <span className="font-semibold tabular">{moment(check.nextAt, now, timeZone)}</span>{" "}
            <span className="text-[13px] text-text-2">every {CHECK_INTERVAL_HOURS} hours</span>
          </span>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The takedown clock: how long since the first abuse report, where the case stands against
 * the next escalation, and what the follow-up checks found. Hidden until a report goes out.
 */
export function TakedownProgress({ detail }: { detail: Detail }) {
  const { timeZone } = usePanel();
  const now = useNow();
  const progress = takedownProgress(detail, now);
  if (!progress) return null;
  const check = siteCheck(detail);
  const reported = moment(progress.reportedAt, now, timeZone);

  if (progress.kind === "offline") {
    return (
      <Card aria-labelledby="progress-title">
        <PanelHeader
          id="progress-title"
          title="Takedown progress"
          description={`From our first report to ${progress.reportedTo} at ${reported} to the first check that found the site offline.`}
        />
        <PanelBody className="flex flex-wrap items-end gap-x-10 gap-y-3">
          <div>
            <p className="text-[13px] text-text-2">Time from report to offline</p>
            <Hours
              value={progress.hours}
              minutes
              className="font-display text-[30px] leading-[1.1] font-semibold"
            />
          </div>
          <p className="text-[13px] text-text-2">
            Offline since {moment(progress.offlineAt, now, timeZone)}
            {progress.takenDownAt
              ? ` · counted as taken down ${moment(progress.takenDownAt, now, timeZone)}, after 72 hours offline`
              : " · counted as taken down after 72 hours offline"}
          </p>
        </PanelBody>
      </Card>
    );
  }

  const escalation = progress.escalation;
  const description = escalation
    ? escalation.target
      ? `Time since we reported the site to ${progress.reportedTo} at ${reported}. If it is still online on ${dayTime(escalation.at, timeZone)}, we report it to ${escalation.target}.`
      : `Time since we reported the site to ${progress.reportedTo} at ${reported}. If it is still online on ${dayTime(escalation.at, timeZone)}, the Kalkan team decides the next step with you.`
    : `Time since we reported the site to ${progress.reportedTo} at ${reported}.`;

  return (
    <Card aria-labelledby="progress-title">
      <PanelHeader id="progress-title" title="Takedown progress" description={description} />
      <PanelBody className="grid grid-cols-1 items-center gap-x-8 gap-y-4 min-[768px]:grid-cols-2 min-[1181px]:grid-cols-[180px_minmax(0,1fr)_190px]">
        <div>
          <p className="text-[13px] text-text-2">Time since abuse report</p>
          <Hours
            value={progress.elapsed}
            minutes
            tabular={false}
            className="font-display text-[30px] leading-[1.1] font-semibold"
          />
        </div>
        {escalation ? (
          <div className="min-w-0 min-[768px]:col-span-2 min-[768px]:row-start-2 min-[1181px]:col-span-1 min-[1181px]:row-start-auto">
            <Track progress={progress} now={now} timeZone={timeZone} />
          </div>
        ) : null}
        {check ? <Checks check={check} now={now} timeZone={timeZone} /> : null}
      </PanelBody>
    </Card>
  );
}
