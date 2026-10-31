"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

export type Patch = Record<string, string | null | undefined>;

/** A copy of the query with the patch applied: null or "" removes a key. Keys stay sorted. */
export function withParams(current: URLSearchParams | string, patch: Patch): string {
  const next = new URLSearchParams(current);
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) continue;
    if (value === null || value === "") next.delete(key);
    else next.set(key, value);
  }
  next.sort();
  const query = next.toString();
  return query ? `?${query}` : "";
}

/** Only same-site paths are followed after sign-in. */
export function safeNext(next: string | null | undefined, fallback = "/overview"): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) {
    return fallback;
  }
  if (
    ["/login", "/invite", "/forgot-password", "/reset-password"].some((p) => next.startsWith(p))
  ) {
    return fallback;
  }
  return next;
}

/** The brand and range follow the user from page to page; page filters do not. */
export const SHARED_PARAMS = ["brand", "range", "from", "to"] as const;

export function sharedQuery(params: URLSearchParams): string {
  const shared = new URLSearchParams();
  for (const key of SHARED_PARAMS) {
    const value = params.get(key);
    if (value) shared.set(key, value);
  }
  return withParams(shared, {});
}

/** The page's query string as state; updates replace the history entry. */
export function useUrlState() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const update = useCallback(
    (patch: Patch) => {
      router.replace(`${pathname}${withParams(params, patch)}`, { scroll: false });
    },
    [params, pathname, router],
  );
  return { params, update };
}
