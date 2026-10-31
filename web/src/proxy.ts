import { timingSafeEqual } from "node:crypto";

import { NextResponse, type NextRequest } from "next/server";

import { REPORT_TOKEN_HEADER } from "@/lib/report";

/**
 * Runs before every page (architecture.md §18, §22):
 * - with EDGE_SHARED_SECRET set (prod), a request without the edge Worker's X-Edge-Auth gets
 *   403, so the run.app address serves nothing on its own;
 * - a page outside sign-in without the API's session cookie redirects to /login (the API still
 *   decides; a stale cookie ends in a 401 the client turns into the same redirect);
 * - the invitation e-mail's /invite?token= link moves to /invite/<token>;
 * - the monthly report's print page (§18, §21) takes the worker's X-Report-Token header instead
 *   of the cookie: without it the answer is 401; any other /print path is 404.
 */

export const SESSION_COOKIE = "kalkan_session";
const PRINT_PAGE = /^\/print\/report\/[^/]+$/;
const PUBLIC_PREFIXES = ["/login", "/invite", "/forgot-password", "/reset-password"];
// Files the sign-in pages load before there is a session.
const ASSETS = new Set(["/favicon.ico", "/icon.svg"]);

function sameSecret(given: string | null, expected: string): boolean {
  if (given === null) return false;
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function isPublic(pathname: string): boolean {
  return PUBLIC_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

export function proxy(request: NextRequest) {
  const secret = process.env.EDGE_SHARED_SECRET;
  if (secret && !sameSecret(request.headers.get("x-edge-auth"), secret)) {
    return new NextResponse("Forbidden", { status: 403 });
  }

  const { pathname, searchParams } = request.nextUrl;
  if (pathname.startsWith("/_next/") || ASSETS.has(pathname)) {
    return NextResponse.next();
  }

  if (pathname === "/print" || pathname.startsWith("/print/")) {
    if (!PRINT_PAGE.test(pathname)) {
      return new NextResponse("Not found", { status: 404 });
    }
    if (!request.headers.get(REPORT_TOKEN_HEADER)) {
      return new NextResponse("A report token is required", {
        status: 401,
        headers: { "Cache-Control": "no-store" },
      });
    }
    const printed = NextResponse.next();
    printed.headers.set("Cache-Control", "no-store");
    printed.headers.set("X-Robots-Tag", "noindex");
    return printed;
  }

  if (pathname === "/invite" && searchParams.get("token")) {
    const url = request.nextUrl.clone();
    url.pathname = `/invite/${encodeURIComponent(searchParams.get("token")!)}`;
    url.search = "";
    return NextResponse.redirect(url);
  }

  if (!isPublic(pathname) && !request.cookies.has(SESSION_COOKIE)) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = "";
    if (pathname !== "/") {
      url.searchParams.set("next", pathname + request.nextUrl.search);
    }
    return NextResponse.redirect(url);
  }

  const response = NextResponse.next();
  response.headers.set("Cache-Control", "private, no-store");
  return response;
}

export const config = {
  // /api/* never reaches the web service in prod; locally the rewrite in next.config.ts serves it.
  matcher: ["/((?!api/).*)"],
};
