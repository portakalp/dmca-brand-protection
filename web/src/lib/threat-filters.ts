import type { Schemas } from "@/lib/api/client";
import { THREAT_SORTS, type ThreatSort } from "@/lib/api/queries";
import { CATEGORY_LABELS } from "@/lib/format";
import { STATUSES } from "@/lib/status";

export const PAGE_SIZE = 25;
export const DEFAULT_SORT: ThreatSort = "-first_seen_at";

export type ThreatFilters = {
  status: Schemas["PanelStatus"] | null;
  category: Schemas["Category"] | null;
  q: string | null;
  sort: ThreatSort;
  page: number;
};

function oneOf<T extends string>(value: string | null, options: readonly T[]): T | null {
  return value !== null && (options as readonly string[]).includes(value) ? (value as T) : null;
}

/** The Threats page's filters from the URL; unknown values are ignored. */
export function parseFilters(params: URLSearchParams): ThreatFilters {
  const page = Number.parseInt(params.get("page") ?? "1", 10);
  const q = params.get("q")?.trim().slice(0, 200) || null;
  return {
    status: oneOf(params.get("status"), STATUSES),
    category: oneOf(params.get("category"), Object.keys(CATEGORY_LABELS) as Schemas["Category"][]),
    q,
    sort: oneOf(params.get("sort"), THREAT_SORTS) ?? DEFAULT_SORT,
    page: Number.isFinite(page) && page > 0 ? page : 1,
  };
}

export function hasFilters(filters: ThreatFilters): boolean {
  return filters.status !== null || filters.category !== null || filters.q !== null;
}

export function pageCount(total: number, pageSize = PAGE_SIZE): number {
  return Math.max(1, Math.ceil(total / pageSize));
}

/** "Showing 26–50 of 128". */
export function showing(page: number, total: number, pageSize = PAGE_SIZE): string {
  if (total === 0) return "No threats";
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(total, page * pageSize);
  return `Showing ${first}–${last} of ${total}`;
}
