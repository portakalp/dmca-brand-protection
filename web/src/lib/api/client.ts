import createClient, { type Middleware } from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

/** An RFC 9457 problem from the API (§17): `code` is the machine-readable reason. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string | null,
    readonly detail: string | null,
    readonly body: Record<string, unknown>,
  ) {
    super(detail ?? `Request failed (${status})`);
    this.name = "ApiError";
  }
}

export const CSRF_HEADER = "X-Kalkan-CSRF";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Every state change carries the CSRF header the API demands (§17). */
export const csrf: Middleware = {
  onRequest({ request }) {
    if (!SAFE_METHODS.has(request.method)) {
      request.headers.set(CSRF_HEADER, "1");
    }
    return request;
  },
};

/** Where an unauthenticated request sends the browser: the second factor or the sign-in page. */
export function signInTarget(code: string | null, here: string): string {
  if (code === "mfa_required") {
    return `/login/mfa?next=${encodeURIComponent(here)}`;
  }
  return `/login?next=${encodeURIComponent(here)}`;
}

/** A 401 outside the sign-in endpoints ends the session in the browser too. */
export const unauthorized: Middleware = {
  async onResponse({ request, response }) {
    if (response.status !== 401 || typeof window === "undefined") {
      return response;
    }
    if (new URL(request.url).pathname.startsWith("/api/v1/auth/")) {
      return response;
    }
    const body = (await response
      .clone()
      .json()
      .catch(() => ({}))) as { code?: string };
    const here = window.location.pathname + window.location.search;
    window.location.assign(signInTarget(body.code ?? null, here));
    return response;
  },
};

function baseUrl(): string {
  // Same origin in the browser; tests run without a window.
  return typeof window === "undefined" ? "http://localhost" : window.location.origin;
}

export const api = createClient<paths>({ baseUrl: baseUrl(), credentials: "same-origin" });
api.use(csrf, unauthorized);

/** Unwraps an openapi-fetch result: the data, or an ApiError. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) {
    const body = (result.error ?? {}) as Record<string, unknown>;
    throw new ApiError(
      result.response.status,
      typeof body.code === "string" ? body.code : null,
      typeof body.detail === "string"
        ? body.detail
        : typeof body.title === "string"
          ? body.title
          : null,
      body,
    );
  }
  return result.data as T;
}
