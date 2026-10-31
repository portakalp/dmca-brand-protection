"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api, unwrap, type Schemas } from "./client";

export type OverviewQuery = { brand: string | null; from: string | null; to: string | null };

export type ThreatsQuery = {
  brand: string | null;
  from: string | null;
  to: string | null;
  status: Schemas["PanelStatus"] | null;
  category: Schemas["Category"] | null;
  q: string | null;
  sort: ThreatSort;
  page: number;
  pageSize: number;
};

export const THREAT_SORTS = [
  "-first_seen_at",
  "first_seen_at",
  "domain",
  "-last_action_at",
] as const;
export type ThreatSort = (typeof THREAT_SORTS)[number];

export const keys = {
  me: ["me"] as const,
  overview: (query: OverviewQuery) => ["overview", query] as const,
  threats: (query: ThreatsQuery) => ["threats", query] as const,
  threatCount: (brand: string | null, status: Schemas["PanelStatus"]) =>
    ["threat-count", brand, status] as const,
  threat: (id: string) => ["threat", id] as const,
  whitelist: ["whitelist"] as const,
  reports: ["reports"] as const,
  users: ["users"] as const,
  settings: ["settings"] as const,
};

export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: async () => unwrap(await api.GET("/api/v1/me")),
    staleTime: 60_000,
  });
}

export function useOverview(query: OverviewQuery, enabled = true) {
  return useQuery({
    queryKey: keys.overview(query),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/overview", {
          params: {
            query: {
              brand_id: query.brand ?? undefined,
              from: query.from ?? undefined,
              to: query.to ?? undefined,
            },
          },
        }),
      ),
    enabled,
    placeholderData: keepPreviousData,
  });
}

export function threatsParams(query: Omit<ThreatsQuery, "page" | "pageSize">) {
  return {
    brand_id: query.brand ?? undefined,
    from: query.from ?? undefined,
    to: query.to ?? undefined,
    status: query.status ?? undefined,
    category: query.category ?? undefined,
    q: query.q ?? undefined,
    sort: query.sort,
  };
}

// /threats also answers text/csv, so its JSON body is typed by hand here.
type ThreatPage = Schemas["Page_ThreatItem_"];

export function useThreats(query: ThreatsQuery, enabled = true) {
  return useQuery({
    queryKey: keys.threats(query),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/threats", {
          params: {
            query: { ...threatsParams(query), page: query.page, page_size: query.pageSize },
          },
        }),
      ) as ThreatPage,
    enabled,
    placeholderData: keepPreviousData,
  });
}

/** How many threats are in a status now, whatever the period (the live banner's split). */
export function useThreatCount(
  brand: string | null,
  status: Schemas["PanelStatus"],
  enabled = true,
) {
  return useQuery({
    queryKey: keys.threatCount(brand, status),
    queryFn: async () => {
      const page = unwrap(
        await api.GET("/api/v1/threats", {
          params: { query: { brand_id: brand ?? undefined, status, page_size: 1 } },
        }),
      ) as ThreatPage;
      return page.total;
    },
    enabled,
  });
}

export function useThreat(id: string) {
  return useQuery({
    queryKey: keys.threat(id),
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/threats/{case_id}", { params: { path: { case_id: id } } })),
  });
}

export function useWhitelist() {
  return useQuery({
    queryKey: keys.whitelist,
    queryFn: async () => unwrap(await api.GET("/api/v1/whitelist")),
  });
}

export function useReports() {
  return useQuery({
    queryKey: keys.reports,
    queryFn: async () => unwrap(await api.GET("/api/v1/reports")),
  });
}

export function useUsers(enabled = true) {
  return useQuery({
    queryKey: keys.users,
    queryFn: async () => unwrap(await api.GET("/api/v1/users")),
    enabled,
  });
}

export function useSettings(enabled = true) {
  return useQuery({
    queryKey: keys.settings,
    queryFn: async () => unwrap(await api.GET("/api/v1/settings")),
    enabled,
  });
}
