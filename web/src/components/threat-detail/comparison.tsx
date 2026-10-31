"use client";

import { Check, ImageOff, Lock } from "lucide-react";
import { useState } from "react";

import { usePanel } from "@/components/shell/panel";
import { Card, PanelBody, PanelHeader } from "@/components/states";
import { Meter } from "@/components/ui/meter";
import { Segmented } from "@/components/ui/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { matchedSignals, similarity } from "@/lib/evidence";
import { dayTime } from "@/lib/threat-detail";
import { cn } from "@/lib/utils";

type Profile = Schemas["Profile"];

const PROFILES: { value: Profile; label: string }[] = [
  { value: "desktop", label: "Desktop" },
  { value: "mobile", label: "Mobile" },
];

/** A capture in a plain browser frame; a skeleton until the image arrives from the API. */
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
        "overflow-hidden rounded-sm border border-border-strong bg-sunken",
        mobile && "mx-auto w-full max-w-[260px]",
      )}
    >
      <div className="flex items-center gap-1.5 border-b border-border bg-surface px-2.5 py-1.5 font-mono text-[11.5px] text-text-2">
        <Lock aria-hidden="true" strokeWidth={1.5} className="size-3 flex-none" />
        <span className="min-w-0 truncate">{domain}</span>
      </div>
      <div className={cn("relative", mobile ? "aspect-[9/16]" : "aspect-[16/10]")}>
        {url === null || failed === url ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-[12.5px] text-text-3">
            <ImageOff aria-hidden="true" strokeWidth={1.5} className="size-5" />
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
                "absolute inset-0 size-full object-contain object-top",
                loaded === url ? "opacity-100" : "opacity-0",
              )}
            />
          </>
        )}
      </div>
    </div>
  );
}

function SmallHead({ children }: { children: string }) {
  return <h3 className="mb-2 font-sans text-[12.5px] font-semibold text-text-2">{children}</h3>;
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
    <Card aria-labelledby="compare-title">
      <PanelHeader
        id="compare-title"
        title="Side-by-side comparison"
        description={
          capture
            ? `${label} capture · ${dayTime(capture.captured_at, timeZone)}`
            : `No ${label.toLowerCase()} capture yet`
        }
        actions={
          <Segmented label="Capture" options={PROFILES} value={profile} onChange={setProfile} />
        }
      />
      <PanelBody className="grid grid-cols-1 items-start gap-6 min-[561px]:grid-cols-2 min-[1181px]:grid-cols-[minmax(0,1fr)_230px_minmax(0,1fr)]">
        <figure className="m-0 min-w-0">
          <figcaption className="mb-2 flex min-w-0 items-center gap-2 text-[13px] text-text-2">
            Your site
            <span className="truncate font-mono text-text" title={officialDomain}>
              {officialDomain}
            </span>
          </figcaption>
          <Shot
            image={official}
            domain={officialDomain}
            profile={profile}
            alt={`${label} capture of your site, ${officialDomain}`}
          />
        </figure>

        {/* Between the shots on a wide screen; under both, side by side, on a narrower one. */}
        <div className="flex min-w-0 flex-col gap-4 min-[561px]:max-[1180px]:col-span-2 min-[561px]:max-[1180px]:row-start-2 min-[561px]:max-[1180px]:grid min-[561px]:max-[1180px]:grid-cols-2 min-[561px]:max-[1180px]:gap-6 min-[1181px]:pt-7">
          <div>
            <SmallHead>Visual similarity</SmallHead>
            <p className="font-display text-[40px] leading-none font-semibold">
              {percent !== null ? `${percent}%` : "—"}
            </p>
            {percent !== null ? <Meter value={percent} tone="neutral" className="mt-2.5" /> : null}
            <p className="mt-1.5 text-[12.5px] text-text-3">
              How closely the two screenshots match.
            </p>
          </div>
          <div>
            <SmallHead>Matched signals</SmallHead>
            {signals.length ? (
              <ul aria-label="Matched signals" className="m-0 flex list-none flex-col gap-2 p-0">
                {signals.map((signal) => (
                  <li key={signal} className="flex items-center gap-2 text-[13.5px]">
                    <Check
                      aria-hidden="true"
                      strokeWidth={1.5}
                      className="size-4 flex-none text-text-3"
                    />
                    {signal}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[13px] text-text-3">No matched signals yet.</p>
            )}
          </div>
        </div>

        <figure className="m-0 min-w-0">
          <figcaption className="mb-2 flex min-w-0 items-center gap-2 text-[13px] text-text-2">
            Lookalike
            <span className="truncate font-mono text-text" title={detail.threat.domain}>
              {detail.threat.domain}
            </span>
          </figcaption>
          <Shot
            image={clone}
            domain={detail.threat.domain}
            profile={profile}
            alt={`${label} capture of the clone, ${detail.threat.domain}`}
          />
        </figure>
      </PanelBody>
    </Card>
  );
}
