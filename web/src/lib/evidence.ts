import type { Schemas } from "@/lib/api/client";

type File = Schemas["FileOut"];
type Signal = Schemas["SignalOut"];

/** "1.2 MB", "640 KB", "3 KB" (decimal units, like the design's evidence list). */
export function fileSize(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} KB`;
  const mb = bytes / 1_000_000;
  return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
}

const BASE_NAMES: Record<Schemas["ArtifactKind"], (profile: string | null) => string> = {
  screenshot: (profile) => `${profile ?? "page"}.png`,
  screenshot_full: (profile) => `${profile ?? "page"}-full.png`,
  html: (profile) => (profile === "mobile" ? "page-mobile.html" : "page.html"),
  har: (profile) => (profile === "mobile" ? "session-mobile.har" : "session.har"),
  dns: () => "dns.json",
  rdap: () => "rdap.json",
  tls: () => "tls.json",
  favicon: () => "favicon.ico",
};

/** The design's short file names; a second capture of the same kind gets a number. */
export function fileNames(files: File[]): Map<string, string> {
  const names = new Map<string, string>();
  const used = new Map<string, number>();
  for (const file of files) {
    const base = BASE_NAMES[file.kind](file.profile ?? null);
    const seen = used.get(base) ?? 0;
    used.set(base, seen + 1);
    names.set(file.id, seen === 0 ? base : base.replace(/(\.[^.]+)$/, `-${seen + 1}$1`));
  }
  return names;
}

/** Screenshots first (desktop, then mobile), then the page, the network log and lookups. */
const ORDER: Schemas["ArtifactKind"][] = [
  "screenshot",
  "screenshot_full",
  "html",
  "har",
  "rdap",
  "dns",
  "tls",
  "favicon",
];

export function sortFiles(files: File[]): File[] {
  return [...files].sort(
    (a, b) =>
      ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind) ||
      (a.profile ?? "").localeCompare(b.profile ?? "") ||
      a.captured_at.localeCompare(b.captured_at),
  );
}

/** The earliest day a file of the package may be deleted (every file is kept 180 days). */
export function retainedUntil(files: File[]): string | null {
  return files.reduce<string | null>(
    (first, file) => (first === null || file.retained_until < first ? file.retained_until : first),
    null,
  );
}

/** The similarity as a whole percentage, from the comparison or else the signal. */
export function similarity(detail: Schemas["ThreatDetail"]): number | null {
  const value =
    detail.comparison.visual_similarity ??
    detail.signals.find((signal) => signal.name === "visual_similarity")?.value;
  return typeof value === "number" ? Math.round(value * 100) : null;
}

const FILE_LABELS: Record<Schemas["ArtifactKind"], string> = {
  screenshot: "screenshot",
  screenshot_full: "full-page screenshot",
  html: "Page source",
  har: "Network log",
  rdap: "Registration record",
  dns: "DNS records",
  tls: "Certificate",
  favicon: "Site icon",
};

/** "Desktop screenshot", "Page source": what a file of the package is. */
export function fileLabel(file: File): string {
  const label = FILE_LABELS[file.kind];
  if (file.kind !== "screenshot" && file.kind !== "screenshot_full") return label;
  const profile =
    file.profile === "mobile" ? "Mobile" : file.profile === "desktop" ? "Desktop" : "Page";
  return `${profile} ${label}`;
}

/** "3f9a62d1c07be44a19e8f3c2…0e7bc21e": a hash's head and tail; the full one goes in a title. */
export function shortHash(hash: string, head = 24, tail = 8): string {
  return hash.length <= head + tail + 1 ? hash : `${hash.slice(0, head)}…${hash.slice(-tail)}`;
}

/**
 * The signals a customer understands, in the design's order. A boolean counts when true; a
 * score counts from its threshold (brand in domain from 80 of 100, page layout from
 * 0.85 visual similarity or 80 text similarity).
 */
const MATCHERS: { label: string; matches: (values: Map<string, Signal["value"]>) => boolean }[] = [
  { label: "Logo match", matches: (v) => v.get("logo_match") === true },
  {
    label: "Same page layout",
    matches: (v) =>
      atLeast(v.get("visual_similarity"), 0.85) || atLeast(v.get("text_similarity"), 80),
  },
  { label: "Identical favicon", matches: (v) => v.get("favicon_match") === true },
  { label: "Brand name in domain", matches: (v) => atLeast(v.get("brand_in_domain"), 80) },
  { label: "Brand name in title", matches: (v) => v.get("brand_in_title") === true },
  { label: "Asks for card details", matches: (v) => v.get("form_card") === true },
  { label: "Sends the form elsewhere", matches: (v) => v.get("external_form_action") === true },
  { label: "Listed by Google Web Risk", matches: (v) => v.get("webrisk_listed") === true },
];

function atLeast(value: Signal["value"] | undefined, threshold: number): boolean {
  return typeof value === "number" && value >= threshold;
}

export function matchedSignals(signals: Signal[], limit = 4): string[] {
  const values = new Map(signals.map((signal) => [signal.name, signal.value]));
  return MATCHERS.filter((matcher) => matcher.matches(values))
    .slice(0, limit)
    .map((matcher) => matcher.label);
}
