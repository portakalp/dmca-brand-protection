"use client";

import { Check, ImageOff, Lock, Monitor, Smartphone } from "lucide-react";
import { useState } from "react";

import { usePanel } from "@/components/shell/panel";
import { Card, CardTitle } from "@/components/states";
import { Pill } from "@/components/pill";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { matchedSignals, ringDash, similarity } from "@/lib/evidence";
import { dayTime } from "@/lib/threat-detail";
import { cn } from "@/lib/utils";

type Profile = Schemas["Profile"];

const PROFILES: { value: Profile; label: string; icon: typeof Monitor }[] = [
  { value: "desktop", label: "Desktop", icon: Monitor },
  { value: "mobile", label: "Mobile", icon: Smartphone },
];

/** A capture in a browser frame; a skeleton until the image arrives from the API. */
function Shot({
  image,
  domain,
  profile,
  alt,
}: {
  image: Schemas["Image"] | undefined;
  domain: string;
  profile: Profile;
  alt: string;
}) {
  const [loaded, setLoaded] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const url = image?.url ?? null;
  const mobile = profile === "mobile";
  return (
    <div
      className={cn(
        "overflow-hidden rounded-[12px] border border-control-border bg-segment shadow-xs",
        mobile && "mx-auto w-full max-w-[240px]",
      )}
    >
      <div className="flex h-[34px] items-center border-b border-control-border bg-segment px-2">
        <span className="flex h-[22px] min-w-0 flex-auto items-center gap-1.5 overflow-hidden rounded-[6px] bg-panel px-2 font-mono text-[11px] whitespace-nowrap text-text-secondary">
          <Lock aria-hidden="true" className="size-[11px] flex-none" />
          {domain}
        </span>
      </div>
      <div className={cn("relative", mobile ? "aspect-[9/16]" : "aspect-[7/5]")}>
        {url === null || failed === url ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-[12.5px] text-text-tertiary">
            <ImageOff aria-hidden="true" className="size-5" />
            {url === null ? "No capture yet" : "The capture could not be loaded"}
          </div>
        ) : (
          <>
            {loaded !== url ? (
              <Skeleton aria-hidden="true" className="absolute inset-0 rounded-none" />
            ) : null}
            {/* Evidence comes from the API as is; next/image would re-encode it. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              key={url}
              src={url}
              alt={alt}
              loading="lazy"
              onLoad={() => setLoaded(url)}
              onError={() => setFailed(url)}
              className={cn(
                "absolute inset-0 size-full object-cover object-top transition-opacity",
                loaded === url ? "opacity-100" : "opacity-0",
              )}
            />
          </>
        )}
      </div>
    </div>
  );
}

function Ring({ percent }: { percent: number | null }) {
  return (
    <div className="relative size-[132px]">
      <svg width="132" height="132" viewBox="0 0 132 132" aria-hidden="true" className="block">
        <circle cx="66" cy="66" r="54" fill="none" stroke="var(--card-border)" strokeWidth="10" />
        {percent !== null ? (
          <circle
            cx="66"
            cy="66"
            r="54"
            fill="none"
            stroke="var(--accent-brand)"
            strokeWidth="10"
            strokeLinecap="round"
            strokeDasharray={ringDash(percent)}
            transform="rotate(-90 66 66)"
          />
        ) : null}
      </svg>
      <p className="absolute inset-0 m-0 flex flex-col items-center justify-center">
        <span className="tabular text-[28px] leading-8 font-semibold tracking-[-0.02em]">
          {percent !== null ? `${percent}%` : "—"}
        </span>
        <span className="text-xs leading-4 text-text-tertiary">similarity</span>
      </p>
    </div>
  );
}

export function Comparison({ detail }: { detail: Schemas["ThreatDetail"] }) {
  const { timeZone } = usePanel();
  const [profile, setProfile] = useState<Profile>("desktop");
  const official = detail.comparison.official.find((image) => image.profile === profile);
  const clone = detail.comparison.clone.find((image) => image.profile === profile);
  const capture = detail.files.find(
    (file) => file.kind === "screenshot" && file.profile === profile,
  );
  const percent = similarity(detail);
  const signals = matchedSignals(detail.signals);
  const officialDomain = detail.official_domain ?? detail.brand;
  const label = profile === "desktop" ? "Desktop" : "Mobile";

  return (
    <Card aria-labelledby="compare-title" className="flex flex-col gap-[18px] px-5 pt-[18px] pb-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <CardTitle
          id="compare-title"
          title="Side-by-side comparison"
          subtitle={
            capture
              ? `${label} capture · ${dayTime(capture.captured_at, timeZone)}`
              : `No ${label.toLowerCase()} capture yet`
          }
        />
        <div
          role="group"
          aria-label="Capture"
          className="inline-flex gap-0.5 rounded-control bg-segment p-[3px]"
        >
          {PROFILES.map((item) => {
            const Icon = item.icon;
            const pressed = profile === item.value;
            return (
              <button
                key={item.value}
                type="button"
                aria-pressed={pressed}
                onClick={() => setProfile(item.value)}
                className={cn(
                  "inline-flex min-h-[34px] items-center gap-1.5 rounded-[8px] px-3 text-[13px] font-medium",
                  pressed
                    ? "bg-panel text-text shadow-segment"
                    : "text-text-secondary hover:text-text",
                )}
              >
                <Icon aria-hidden="true" className="size-[15px]" />
                {item.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex flex-wrap items-stretch gap-5">
        <figure className="m-0 flex min-w-0 flex-[1_1_300px] flex-col gap-2.5">
          <figcaption className="flex min-h-[22px] flex-wrap items-center gap-2 text-[13px]">
            <Pill>Your site</Pill>
            <span className="font-mono text-[12.5px] font-medium">{officialDomain}</span>
          </figcaption>
          <Shot
            image={official}
            domain={officialDomain}
            profile={profile}
            alt={`${label} capture of your site, ${officialDomain}`}
          />
        </figure>

        <div className="mx-auto flex min-w-0 flex-[0_1_200px] flex-col items-center justify-center gap-[18px] py-2">
          <Ring percent={percent} />
          {signals.length ? (
            <ul aria-label="Matched signals" className="m-0 flex list-none flex-col gap-[9px] p-0">
              {signals.map((signal) => (
                <li key={signal} className="flex items-center gap-2 text-[13px] text-text-strong">
                  <span
                    aria-hidden="true"
                    className="inline-flex size-[18px] flex-none items-center justify-center rounded-full bg-danger-bg text-danger-fg"
                  >
                    <Check className="size-[11px]" strokeWidth={3} />
                  </span>
                  {signal}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-center text-[13px] text-text-tertiary">No matched signals yet</p>
          )}
        </div>

        <figure className="m-0 flex min-w-0 flex-[1_1_300px] flex-col gap-2.5">
          <figcaption className="flex min-h-[22px] flex-wrap items-center gap-2 text-[13px]">
            <Pill tone="danger">Clone</Pill>
            <span className="font-mono text-[12.5px] font-medium">{detail.threat.domain}</span>
          </figcaption>
          <Shot
            image={clone}
            domain={detail.threat.domain}
            profile={profile}
            alt={`${label} capture of the clone, ${detail.threat.domain}`}
          />
        </figure>
      </div>
    </Card>
  );
}
