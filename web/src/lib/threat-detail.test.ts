import { describe, expect, it } from "vitest";

import type { Schemas } from "./api/client";
import {
  capturedText,
  noticeRows,
  noticesCaption,
  sameDay,
  shortOrg,
  timelineSteps,
  zoneName,
} from "./threat-detail";

const ZONE = "Europe/Warsaw";

/** The design's case: two notices out, the registry due two days after the last. */
function detail(overrides: Partial<Schemas["ThreatDetail"]> = {}): Schemas["ThreatDetail"] {
  const file = (kind: Schemas["ArtifactKind"], profile: Schemas["Profile"] | null) =>
    ({ id: `${kind}-${profile}`, kind, profile }) as Schemas["FileOut"];
  return {
    threat: {
      id: "t",
      number: "KLK-1",
      domain: "mintera-login.com",
      domain_unicode: null,
      brand_id: "b",
      category: "phishing_clone",
      status: "notice_sent",
      first_seen_at: "2026-10-08T07:14:00Z",
      hosting: "Cloudflare",
      last_action: null,
      last_action_at: null,
    },
    brand: "Mintera Bank",
    official_domain: "mintera.com",
    source: "ct",
    comparison: { official: [], clone: [], visual_similarity: 0.94 },
    signals: [],
    ai_review: {
      reviews: [
        {
          source: "ai_confirm",
          category: "phishing_clone",
          confidence: 0.97,
          short_explanation: "x",
          created_at: "2026-10-08T07:21:00Z",
        },
      ],
      rule: { outcome: "approve", rule: "auto_approve", created_at: "2026-10-08T07:21:00Z" },
    },
    infrastructure: {
      registered_at: null,
      registrar: "Namecheap, Inc.",
      nameservers: [],
      proxy: "Cloudflare",
      hosting: "Cloudflare",
      hosting_country: "US",
      origin_host: "Hetzner Online GmbH",
      origin_country: "DE",
      ips: [],
      tls: null,
    },
    timeline: [
      { type: "detected", at: "2026-10-08T07:14:00Z", detail: {} },
      { type: "captured", at: "2026-10-08T07:16:00Z", detail: {} },
      { type: "verified", at: "2026-10-08T07:21:00Z", detail: {} },
      { type: "approved", at: "2026-10-08T07:21:00Z", detail: {} },
      { type: "notice_sent", at: "2026-10-08T07:40:00Z", detail: { channel: "cloudflare" } },
      { type: "notice_sent", at: "2026-10-08T08:05:00Z", detail: { channel: "hosting" } },
    ],
    notices: [
      {
        id: "n1",
        channel: "cloudflare",
        method: "api",
        recipient: null,
        template: "phishing",
        language: "en",
        status: "acknowledged",
        escalation_level: 0,
        round: 1,
        sent_at: "2026-10-08T07:40:00Z",
      },
      {
        id: "n2",
        channel: "hosting",
        method: "email",
        recipient: "abuse@hetzner.example",
        template: "phishing",
        language: "en",
        status: "sent",
        escalation_level: 0,
        round: 1,
        sent_at: "2026-10-08T08:05:00Z",
      },
    ],
    files: [
      file("screenshot", "desktop"),
      file("screenshot", "mobile"),
      file("html", "desktop"),
      file("har", "desktop"),
    ],
    next_steps: {
      next_check_at: "2026-10-08T14:05:00Z",
      escalation_at: "2026-10-10T08:05:00Z",
      escalation_channel: "registry",
    },
    evidence_zip_url: "/api/v1/threats/t/evidence.zip",
    ...overrides,
  };
}

describe("threat detail", () => {
  it("tells the design's timeline, ending with what happens next", () => {
    const steps = timelineSteps(detail(), ZONE);
    expect(steps.map((step) => step.title)).toEqual([
      "Detected",
      "Captured",
      "Verified",
      "Reported to Cloudflare",
      "Host notified",
      "Waiting for takedown",
    ]);
    expect(steps[1].text).toBe(
      "Desktop and mobile screenshots, page source and network log saved.",
    );
    expect(steps[2].text).toBe("Classified as a phishing clone with 0.97 confidence.");
    expect(steps[4].text).toBe(
      "Cloudflare disclosed the origin host, and its abuse desk was emailed.",
    );
    expect(steps.at(-1)).toMatchObject({
      at: null,
      tone: "current",
      text: "The site is checked every 6 hours; next check at 16:05. If it is still live on Oct 10 at 10:05, the case goes to the registry.",
    });
  });

  it("marks warning pages and stops at a closed case", () => {
    const steps = timelineSteps(
      detail({
        threat: { ...detail().threat, status: "taken_down" },
        timeline: [
          { type: "warning_active", at: "2026-10-09T07:00:00Z", detail: {} },
          { type: "taken_down", at: "2026-10-12T07:00:00Z", detail: {} },
        ],
      }),
      ZONE,
    );
    expect(steps.map((step) => [step.title, step.tone])).toEqual([
      ["Warning page in front of the site", "warning"],
      ["Taken down", "done"],
    ]);
  });

  it("lists the notices and the scheduled escalation", () => {
    const rows = noticeRows(detail(), ZONE);
    expect(rows.map((row) => [row.title, row.subtitle, row.status])).toEqual([
      ["Cloudflare abuse report", "Phishing form · Oct 8, 09:40", "Received"],
      ["Hetzner Online abuse desk", "Email · Oct 8, 10:05", "Awaiting reply"],
      ["Registry of .com", "Escalation · Oct 10, 10:05", "Scheduled"],
    ]);
    expect(noticesCaption(rows, detail())).toBe("2 sent · 1 scheduled");
  });

  it("shortens names and times", () => {
    expect(shortOrg("Namecheap, Inc.")).toBe("Namecheap");
    expect(shortOrg("Hetzner Online GmbH")).toBe("Hetzner Online");
    expect(zoneName(ZONE, "2026-10-08T07:14:00Z")).toBe("CEST");
    expect(sameDay(["2026-10-08T07:14:00Z", "2026-10-08T21:59:00Z"], ZONE)).toBe(true);
    expect(sameDay(["2026-10-08T07:14:00Z", "2026-10-08T22:01:00Z"], ZONE)).toBe(false);
    expect(capturedText([])).toBe("The page was captured.");
  });
});
