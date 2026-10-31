/**
 * Edge Worker in front of the Cloud Run services (architecture.md §22; K80, K84).
 *
 * app.<domain>/api/* and evidence.<domain>/* go to `api`, the rest of app.<domain> to `web`.
 * Every forwarded request carries X-Edge-Auth; without the secret or a usable origin the
 * Worker answers 503 itself (fail closed), and it never contacts any other host. Responses go
 * back as is, except that a Location on an origin's host is moved to the public host.
 */

export type Upstream = "api" | "web";

export interface Route {
  upstream: Upstream;
  /** Path on the origin; differs from the public path only for the health aliases. */
  path: string;
}

/** Cloud Run's front end answers some paths ending in "z" itself, so these go to /api/health (K80). */
const HEALTH_ALIASES: ReadonlySet<string> = new Set(["/healthz", "/api/healthz"]);
const API_HEALTH_PATH = "/api/health";

/** Client-sent copies never reach the origin; the Worker sets its own values below. */
const DROPPED_REQUEST_HEADERS = [
  "host",
  "x-edge-auth",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "forwarded",
];

/** Pure routing table: which origin serves `path` on `host`, or null for hosts we do not serve. */
export function route(
  host: string,
  path: string,
  hosts: { app: string; evidence: string },
): Route | null {
  const name = host.toLowerCase();
  const isApp = hosts.app !== "" && name === hosts.app.toLowerCase();
  const isEvidence = hosts.evidence !== "" && name === hosts.evidence.toLowerCase();
  if (!isApp && !isEvidence) {
    return null;
  }
  if (HEALTH_ALIASES.has(path)) {
    return { upstream: "api", path: API_HEALTH_PATH };
  }
  if (isEvidence || path === "/api" || path.startsWith("/api/")) {
    return { upstream: "api", path };
  }
  return { upstream: "web", path };
}

/** An origin we may send the secret to: an absolute https URL; anything else is unusable. */
export function parseOrigin(value: string | undefined): URL | null {
  if (!value) {
    return null;
  }
  try {
    const origin = new URL(value);
    return origin.protocol === "https:" ? origin : null;
  } catch {
    return null;
  }
}

/**
 * Location for the client, or null to leave it as is. FastAPI builds absolute redirects
 * (redirect_slashes) from the Host it sees, the run.app name, and uvicorn behind Cloud Run sees
 * http; run.app answers 403 without the edge header. A Location on an origin's host (http, https
 * or protocol-relative) moves to the public host with path and query unchanged (K84).
 */
export function publicLocation(
  location: string,
  publicHost: string,
  originHosts: readonly string[],
): string | null {
  let target: URL;
  try {
    // Resolved against the public host, so relative Locations never match an origin.
    target = new URL(location, `https://${publicHost}`);
  } catch {
    return null;
  }
  if (target.protocol !== "https:" && target.protocol !== "http:") {
    return null;
  }
  if (!originHosts.includes(target.host)) {
    return null;
  }
  return `https://${publicHost}${target.pathname}${target.search}${target.hash}`;
}

function plain(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
  });
}

export async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const target = route(url.hostname, url.pathname, { app: env.APP_HOST, evidence: env.EVIDENCE_HOST });
  if (target === null) {
    return plain(404, "Not found");
  }
  if (url.protocol !== "https:") {
    // X-Forwarded-Proto below always says https; plain-HTTP clients are sent there first.
    url.protocol = "https:";
    return Response.redirect(url.toString(), 308);
  }

  const originValue = target.upstream === "api" ? env.API_ORIGIN : env.WEB_ORIGIN;
  if (target.upstream === "web" && !originValue) {
    return plain(503, "web is not deployed yet");
  }
  const origin = parseOrigin(originValue);
  const secret = env.EDGE_SHARED_SECRET;
  if (origin === null || !secret) {
    return plain(503, "edge is not configured");
  }

  // Clone the public URL and swap only scheme and host, so path and query stay byte for byte
  // and a path such as //other.example/x cannot move the request (and the secret) elsewhere.
  const upstreamUrl = new URL(url);
  upstreamUrl.protocol = origin.protocol;
  upstreamUrl.host = origin.host;
  if (target.path !== url.pathname) {
    upstreamUrl.pathname = target.path;
  }

  const headers = new Headers(request.headers);
  for (const name of DROPPED_REQUEST_HEADERS) {
    headers.delete(name);
  }
  headers.set("x-edge-auth", secret);
  headers.set("x-forwarded-host", url.host);
  headers.set("x-forwarded-proto", "https");
  const clientIp = request.headers.get("cf-connecting-ip");
  if (clientIp) {
    headers.set("x-forwarded-for", clientIp);
  }

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const upstreamRequest = new Request(upstreamUrl, {
    method: request.method,
    headers,
    body: hasBody ? request.body : null,
    // Redirects go back to the client; following them would carry X-Edge-Auth to the target.
    redirect: "manual",
    // API answers and evidence files must never come from Cloudflare's cache.
    cache: "no-store",
  });
  let response: Response;
  try {
    response = await fetch(upstreamRequest);
  } catch (error) {
    // No URL in the log: paths carry evidence tokens (K35).
    console.error(
      JSON.stringify({
        event: "edge.upstream_failed",
        upstream: target.upstream,
        error: error instanceof Error ? error.name : "unknown",
      }),
    );
    return plain(502, "Bad gateway");
  }

  // The response goes back as is, except a Location that points at an origin.
  const location = response.headers.get("location");
  const originHosts = [env.API_ORIGIN, env.WEB_ORIGIN]
    .map((value) => parseOrigin(value)?.host)
    .filter((host): host is string => host !== undefined);
  const rewritten = location === null ? null : publicLocation(location, url.host, originHosts);
  if (rewritten === null) {
    return response;
  }
  const redirected = new Response(response.body, response);
  redirected.headers.set("location", rewritten);
  return redirected;
}

export default {
  fetch: (request, env) => handle(request, env),
} satisfies ExportedHandler<Env>;
