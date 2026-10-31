import type { Schemas } from "@/lib/api/client";
import type { IndicatorTone } from "@/lib/status";

import { categoryLabel } from "./format";
import { todayIn } from "./period";

/**
 * The threat detail's words (product.md §9, §11): the timeline, the notices and the header's
 * line. The API sends events and rows; the customer reads what happened and what comes next.
 */

type Detail = Schemas["ThreatDetail"];
type Event = Schemas["EventOut"];
type Notice = Schemas["NoticeOut"];

export type Tone = "done" | "warning" | "current";

export type Step = {
  id: string;
  title: string;
  text: string;
  /** null for the step the case is in now. */
  at: string | null;
  tone: Tone;
};

// --- time ------------------------------------------------------------------------------------

export function clock(at: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(at));
}

/** "Oct 8, 09:14". */
export function dayTime(at: string, timeZone: string): string {
  const day = new Intl.DateTimeFormat("en-US", { timeZone, month: "short", day: "numeric" });
  return `${day.format(new Date(at))}, ${clock(at, timeZone)}`;
}

/** "Oct 8, 2026, 09:14". */
export function fullMoment(at: string, timeZone: string): string {
  const day = new Intl.DateTimeFormat("en-US", {
    timeZone,
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  return `${day.format(new Date(at))}, ${clock(at, timeZone)}`;
}

/** "CEST": the zone's short name at that moment ("GMT+3" where there is none). */
export function zoneName(timeZone: string, at: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone, timeZoneName: "short" }).formatToParts(
    new Date(at),
  );
  return parts.find((part) => part.type === "timeZoneName")?.value ?? timeZone;
}

/** True when every moment falls on one day in the zone. */
export function sameDay(moments: string[], timeZone: string): boolean {
  return new Set(moments.map((at) => todayIn(timeZone, new Date(at)))).size <= 1;
}

/** "97%": a review's confidence (0 to 1) as a whole percentage. */
export function percent(confidence: number): string {
  return `${Math.round(confidence * 100)}%`;
}

/** "Germany" for "DE"; the code itself when the name is unknown. */
export function countryName(code: string): string {
  try {
    return new Intl.DisplayNames("en", { type: "region" }).of(code.toUpperCase()) ?? code;
  } catch {
    return code;
  }
}

// --- the header ------------------------------------------------------------------------------

const SOURCE_FACTS: Record<string, [string, string]> = {
  ct: ["Found in", "certificate logs (CT)"],
  czds: ["Found in", "a new zone file"],
  dnstwist: ["Found among", "lookalike spellings"],
  manual: ["Added by", "the Kalkan team"],
};

/** The header's "Found in certificate logs (CT)", as a label and a value. */
export function sourceFact(source: string | null): { label: string; value: string } | null {
  const fact = source ? SOURCE_FACTS[source] : undefined;
  return fact ? { label: fact[0], value: fact[1] } : null;
}

// --- takedown progress -----------------------------------------------------------------------

/** How often a reported site is checked (product.md §9); the API sends the next check only. */
export const CHECK_INTERVAL_HOURS = 6;

const HOUR = 3_600_000;

/** The first abuse report that went out (withdrawals do not count). */
export function firstReport(detail: Detail): Notice | null {
  return detail.notices
    .filter((notice) => notice.sent_at !== null && notice.template !== "retraction")
    .reduce<Notice | null>(
      (first, notice) => (first === null || notice.sent_at! < first.sent_at! ? notice : first),
      null,
    );
}

/** The latest event that says whether the site answered: offline, or back online. */
function lastReachability(detail: Detail): Event | null {
  const events = detail.timeline.filter(
    (event) => event.type === "offline_detected" || event.type === "online_again",
  );
  return events.length ? events[events.length - 1] : null;
}

export type SiteCheck = {
  /** The last follow-up check: one interval before the next, never before the first report. */
  lastAt: string | null;
  nextAt: string | null;
  /** What the checks found: still online, or offline since an event's time. */
  online: boolean;
  offlineSince: string | null;
};

/** The follow-up checks while a reported case is open; null before a report or once closed. */
export function siteCheck(detail: Detail): SiteCheck | null {
  const report = firstReport(detail);
  if (!report || detail.threat.status !== "notice_sent") return null;
  const nextAt = detail.next_steps.next_check_at;
  let lastAt: string | null = null;
  if (nextAt) {
    const last = Date.parse(nextAt) - CHECK_INTERVAL_HOURS * HOUR;
    if (last >= Date.parse(report.sent_at!)) lastAt = new Date(last).toISOString();
  }
  const reach = lastReachability(detail);
  const offline = reach?.type === "offline_detected";
  return { lastAt, nextAt, online: !offline, offlineSince: offline ? reach.at : null };
}

/** "the registry of .com": where an escalation goes, in a sentence. */
export function escalationTarget(channel: Schemas["Channel"], detail: Detail): string {
  const infra = detail.infrastructure;
  switch (channel) {
    case "registry":
      return `the registry of .${tld(detail.threat.domain)}`;
    case "registrar":
      return infra.registrar ? `the registrar, ${shortOrg(infra.registrar)}` : "the registrar";
    case "hosting": {
      const host = infra.origin_host ?? infra.hosting;
      return host ? `the hosting provider, ${shortOrg(host)}` : "the hosting provider";
    }
    case "cloudflare":
      return "Cloudflare";
    case "google":
      return "Google Safe Browsing";
  }
}

export type Progress =
  | {
      kind: "open";
      reportedAt: string;
      reportedTo: string;
      /** Hours from the first report to now. */
      elapsed: number;
      /** The escalation that ends the track; null when none is scheduled. */
      escalation: { at: string; label: string; target: string | null } | null;
      /** Where now sits on the track, 0 to 1; null without an escalation. */
      position: number | null;
    }
  | {
      kind: "offline";
      reportedAt: string;
      reportedTo: string;
      /** Hours from the first report to the first check that found the site offline. */
      hours: number;
      offlineAt: string;
      takenDownAt: string | null;
    };

const REPORTED_TO: Record<Schemas["Channel"], string> = {
  cloudflare: "Cloudflare",
  hosting: "the hosting provider",
  registrar: "the registrar",
  registry: "the registry",
  google: "Google Safe Browsing",
};

const CHANNEL_TITLES: Record<Schemas["Channel"], string> = {
  cloudflare: "Cloudflare",
  hosting: "Host",
  registrar: "Registrar",
  registry: "Registry",
  google: "Google",
};

/**
 * The takedown clock (product.md §9): from the first abuse report to now while the site is up,
 * or to the first check that found it offline. Null before any report goes out.
 */
export function takedownProgress(detail: Detail, now: Date): Progress | null {
  const report = firstReport(detail);
  if (!report) return null;
  const reportedAt = report.sent_at!;
  const start = Date.parse(reportedAt);
  const reportedTo = REPORTED_TO[report.channel];
  const offline = detail.timeline.find(
    (event) => event.type === "offline_detected" && Date.parse(event.at) >= start,
  );
  const closed = detail.threat.status === "taken_down";
  if (offline && (closed || lastReachability(detail)?.type === "offline_detected")) {
    return {
      kind: "offline",
      reportedAt,
      reportedTo,
      hours: (Date.parse(offline.at) - start) / HOUR,
      offlineAt: offline.at,
      takenDownAt: detail.timeline.find((event) => event.type === "taken_down")?.at ?? null,
    };
  }
  if (detail.threat.status !== "notice_sent" && detail.threat.status !== "active") return null;
  const elapsed = Math.max(0, (now.getTime() - start) / HOUR);
  const steps = detail.next_steps;
  const end = steps.escalation_at ? Date.parse(steps.escalation_at) : null;
  const escalation =
    steps.escalation_at && end !== null && end > start
      ? {
          at: steps.escalation_at,
          label: steps.escalation_channel
            ? `${CHANNEL_TITLES[steps.escalation_channel]} escalation`
            : "Escalation",
          target: steps.escalation_channel
            ? escalationTarget(steps.escalation_channel, detail)
            : null,
        }
      : null;
  const position =
    escalation && end !== null
      ? Math.min(1, Math.max(0, (now.getTime() - start) / (end - start)))
      : null;
  return { kind: "open", reportedAt, reportedTo, elapsed, escalation, position };
}

// --- the timeline ----------------------------------------------------------------------------

const SOURCE_TEXT: Record<string, string> = {
  ct: "A new certificate for the domain appeared in certificate transparency logs.",
  czds: "The domain appeared in a newly published zone file.",
  dnstwist: "Found while checking lookalike spellings of your domain.",
  manual: "The Kalkan team added the domain for review.",
};

/** "Desktop and mobile screenshots, page source and network log saved." */
export function capturedText(files: Schemas["FileOut"][]): string {
  const shots = new Set(files.filter((f) => f.kind === "screenshot").map((f) => f.profile));
  const parts: string[] = [];
  if (shots.has("desktop") && shots.has("mobile")) parts.push("Desktop and mobile screenshots");
  else if (shots.has("desktop")) parts.push("A desktop screenshot");
  else if (shots.has("mobile")) parts.push("A mobile screenshot");
  if (files.some((f) => f.kind === "html"))
    parts.push(parts.length ? "page source" : "Page source");
  if (files.some((f) => f.kind === "har")) parts.push(parts.length ? "network log" : "Network log");
  if (!parts.length) return "The page was captured.";
  const last = parts.pop()!;
  return `${parts.length ? `${parts.join(", ")} and ${last}` : last} saved.`;
}

function latestReview(detail: Detail): Schemas["ReviewOut"] | null {
  const reviews = detail.ai_review.reviews;
  return reviews.length ? reviews[reviews.length - 1] : null;
}

function noticeStep(event: Event, detail: Detail): Pick<Step, "title" | "text"> {
  const channel = event.detail.channel;
  const method = event.detail.method;
  const infra = detail.infrastructure;
  const phishing = detail.threat.category === "phishing_clone";
  switch (channel) {
    case "cloudflare":
      return {
        title: "Reported to Cloudflare",
        text: `${phishing ? "Phishing report" : "Report"} filed with the screenshots and page source attached.`,
      };
    case "hosting":
      return {
        title: "Host notified",
        text:
          infra.proxy && infra.origin_host
            ? `${infra.proxy} disclosed the origin host, and its abuse desk was emailed.`
            : method === "manual_form"
              ? "The hosting provider was reported through its abuse form."
              : "The hosting provider's abuse desk was emailed.",
      };
    case "registrar":
      return {
        title: "Registrar notified",
        text: "The registrar's abuse desk was told about the domain.",
      };
    case "registry":
      return {
        title: "Registry notified",
        text: "The site stayed up, so the registry of the domain's extension was told.",
      };
    case "google":
      return {
        title: "Reported to Google Safe Browsing",
        text: "Browsers warn visitors while the takedown is pending.",
      };
    default:
      return { title: "Notice sent", text: "A takedown notice went out." };
  }
}

function eventStep(event: Event, detail: Detail): Omit<Step, "id" | "at"> | null {
  switch (event.type) {
    case "detected":
      return {
        title: "Detected",
        text: SOURCE_TEXT[detail.source ?? ""] ?? "The domain was found.",
        tone: "done",
      };
    case "captured":
      return { title: "Captured", text: capturedText(detail.files), tone: "done" };
    case "verified": {
      const review = latestReview(detail);
      return {
        title: "Verified",
        text: review
          ? `Classified as ${article(categoryLabel(review.category))} with ${percent(review.confidence)} confidence.`
          : "Confirmed as a threat to your brand.",
        tone: "done",
      };
    }
    case "approved":
      return null; // The step between verified and the first notice; nothing to read.
    case "notice_sent":
      return { ...noticeStep(event, detail), tone: "done" };
    case "registrar_escalated":
      return {
        title: "Escalated",
        text: "The site stayed up, so the case moved to its next step.",
        tone: "done",
      };
    case "reply_received":
      return {
        title: "Provider replied",
        text:
          event.detail.classification === "removed"
            ? "They say the site was removed; Kalkan checks before closing the case."
            : "A reply came in and the team read it.",
        tone: "done",
      };
    case "offline_detected":
      return {
        title: "Site offline",
        text: "The site stopped responding. It counts as taken down after 72 hours offline.",
        tone: "done",
      };
    case "online_again":
      return {
        title: "Back online",
        text: "The site came back after going offline, so the checks go on.",
        tone: "warning",
      };
    case "warning_active":
      return {
        title: "Warning page in front of the site",
        text: "Visitors see a browser or Cloudflare warning first. It protects them but is not a takedown, so the case stays open.",
        tone: "warning",
      };
    case "taken_down":
      return {
        title: "Taken down",
        text: "The site has been offline for 72 hours.",
        tone: "done",
      };
    case "reopened":
      return {
        title: "Reopened",
        text: "The site came back, so the notices go out again with the same evidence.",
        tone: "warning",
      };
    case "watching":
      return {
        title: "Watching",
        text: "The page is parked or empty. It is scanned again every 7 days.",
        tone: "done",
      };
    case "dismissed":
      return {
        title: "Dismissed",
        text: "Not a threat to your brand. It is checked again every 30 days.",
        tone: "done",
      };
    case "whitelisted":
      return {
        title: "Marked as yours",
        text: "The domain was added to your whitelist.",
        tone: "done",
      };
    case "archived":
      return {
        title: "Archived",
        text: "Checks ended after 30 days offline.",
        tone: "done",
      };
    default:
      return null;
  }
}

function article(label: string): string {
  const lower = label.toLowerCase();
  return `${/^[aeiou]/.test(lower) ? "an" : "a"} ${lower}`;
}

const CHANNEL_NAMES: Partial<Record<Schemas["Channel"], string>> = {
  registry: "registry",
  registrar: "registrar",
  hosting: "hosting provider",
  google: "Google",
};

/** The step the case is in now, with what happens next; null once nothing is pending. */
function currentStep(detail: Detail, timeZone: string): Step | null {
  const steps = detail.next_steps;
  switch (detail.threat.status) {
    case "notice_sent": {
      const parts: string[] = [];
      if (steps.next_check_at) {
        parts.push(
          `The site is checked every 6 hours; next check at ${clock(steps.next_check_at, timeZone)}.`,
        );
      } else {
        parts.push("The site is checked every 6 hours.");
      }
      if (steps.escalation_at) {
        const day = new Intl.DateTimeFormat("en-US", {
          timeZone,
          month: "short",
          day: "numeric",
        }).format(new Date(steps.escalation_at));
        const when = `${day} at ${clock(steps.escalation_at, timeZone)}`;
        const channel = steps.escalation_channel ? CHANNEL_NAMES[steps.escalation_channel] : null;
        parts.push(
          channel
            ? `If it is still live on ${when}, the case goes to the ${channel}.`
            : `If it is still live on ${when}, the team decides the next step with you.`,
        );
      }
      return {
        id: "now",
        title: "Waiting for takedown",
        text: parts.join(" "),
        at: null,
        tone: "current",
      };
    }
    case "active":
      return {
        id: "now",
        title: "Notice being prepared",
        text: "The threat is confirmed and its notices are being prepared.",
        at: null,
        tone: "current",
      };
    case "reviewing":
      return {
        id: "now",
        title: "Being reviewed",
        text: "The team is checking the evidence before any notice goes out.",
        at: null,
        tone: "current",
      };
    default:
      return null;
  }
}

export function timelineSteps(detail: Detail, timeZone: string): Step[] {
  const steps: Step[] = [];
  detail.timeline.forEach((event, index) => {
    const step = eventStep(event, detail);
    if (step) steps.push({ ...step, id: `${event.type}-${index}`, at: event.at });
  });
  const now = currentStep(detail, timeZone);
  if (now) steps.push(now);
  return steps;
}

// --- notices ---------------------------------------------------------------------------------

export type NoticeTone = IndicatorTone;

export type NoticeRow = {
  id: string;
  channel: Schemas["Channel"] | null;
  title: string;
  subtitle: string;
  status: string;
  tone: NoticeTone;
};

const LEGAL_SUFFIX = /[,\s]+(inc\.?|llc|ltd\.?|limited|gmbh|ag|b\.v\.|s\.a\.|sas|oy|ab|co\.?)$/i;

/** "Namecheap, Inc." → "Namecheap"; "Hetzner Online GmbH" → "Hetzner Online". */
export function shortOrg(name: string): string {
  let short = name.trim();
  while (LEGAL_SUFFIX.test(short)) short = short.replace(LEGAL_SUFFIX, "");
  return short || name;
}

function tld(domain: string): string {
  return domain.slice(domain.lastIndexOf(".") + 1);
}

function noticeTitle(channel: Schemas["Channel"], detail: Detail): string {
  const infra = detail.infrastructure;
  switch (channel) {
    case "cloudflare":
      return "Cloudflare";
    case "hosting": {
      const host = infra.origin_host ?? infra.hosting;
      return host ? `${shortOrg(host)} abuse desk` : "Hosting provider";
    }
    case "registrar":
      return infra.registrar ? `${shortOrg(infra.registrar)} registrar` : "Registrar";
    case "registry":
      return `Registry of .${tld(detail.threat.domain)}`;
    case "google":
      return "Google Safe Browsing";
  }
}

const NOTICE_STATUS: Record<Schemas["NoticeStatus"], [string, NoticeTone]> = {
  sent: ["Awaiting reply", "warning"],
  acknowledged: ["Acknowledged", "done"],
  resolved: ["Resolved", "good"],
  rejected: ["Declined", "critical"],
  retracted: ["Withdrawn", "done"],
  draft: ["Being prepared", "scheduled"],
  queued: ["Being prepared", "scheduled"],
  blocked: ["Being prepared", "scheduled"],
  manual_pending: ["Being prepared", "scheduled"],
  failed: ["Being prepared", "scheduled"],
};

function methodLabel(notice: Notice): string {
  if (notice.template === "retraction") return "Withdrawal";
  if (notice.method === "email") return "Email";
  return notice.template === "phishing" ? "Phishing form" : "Abuse form";
}

export function noticeRows(detail: Detail, timeZone: string): NoticeRow[] {
  const rows: NoticeRow[] = detail.notices.map((notice) => {
    const [status, tone] = NOTICE_STATUS[notice.status];
    return {
      id: notice.id,
      channel: notice.channel,
      title: noticeTitle(notice.channel, detail),
      subtitle: `${methodLabel(notice)} · ${notice.sent_at ? dayTime(notice.sent_at, timeZone) : "Not sent yet"}`,
      status,
      tone,
    };
  });
  const steps = detail.next_steps;
  if (steps.escalation_at) {
    rows.push({
      id: "escalation",
      channel: steps.escalation_channel,
      title: steps.escalation_channel
        ? noticeTitle(steps.escalation_channel, detail)
        : "Kalkan team review",
      subtitle: `Escalation · ${dayTime(steps.escalation_at, timeZone)}, only if still online`,
      status: "Scheduled",
      tone: "scheduled",
    });
  }
  return rows;
}

/** "2 sent · 1 scheduled". */
export function noticesCaption(rows: NoticeRow[], detail: Detail): string {
  const sent = detail.notices.filter((notice) => notice.sent_at !== null).length;
  const pending = rows.length - sent;
  const parts = [];
  if (sent) parts.push(`${sent} sent`);
  if (pending) parts.push(`${pending} scheduled`);
  return parts.join(" · ") || "No notices yet";
}

/** "Classified as ...": the AI review's footnote, from the rule that decided the case. */
export function ruleNote(detail: Detail): string | null {
  const rule = detail.ai_review.rule;
  if (!rule) return null;
  switch (rule.outcome) {
    case "approve":
      return rule.rule === "auto_approve"
        ? "Both reviews and the rule checks agreed, so the notice went out automatically under your phishing policy."
        : "The Kalkan team approved the notices after checking the evidence.";
    case "confirm":
    case "verify":
    case "review":
      return "The Kalkan team checks this case before any notice goes out.";
    case "watch":
      return "The page is parked or empty, so it is watched instead of reported.";
    case "dismiss":
      return "The reviews found no threat to your brand, so the case was dismissed.";
  }
}

/** What the rule checks decided, in words ("Approved automatically"); not a pass mark. */
export function ruleOutcome(detail: Detail): string {
  const rule = detail.ai_review.rule;
  if (!rule) return "Pending";
  switch (rule.outcome) {
    case "approve":
      return rule.rule === "auto_approve" ? "Approved automatically" : "Approved by the team";
    case "confirm":
    case "verify":
    case "review":
      return "Sent to the Kalkan team";
    case "watch":
      return "Watch, do not report";
    case "dismiss":
      return "Not a threat";
  }
}
