import type { Schemas } from "@/lib/api/client";
import { REPORT_TOKEN_HEADER, isReportId } from "@/lib/report";

export type ReportData = Schemas["ReportData"];

/**
 * The print page's data, read on the server with the worker's token (§17, §21). The token goes
 * to the API in the same header and nowhere else: not to the browser, a URL or a log line.
 * Locally the API is API_ORIGIN (as for the rewrite); in prod the web service has no route of
 * its own to the api service, so it goes through the edge at APP_BASE_URL like any client.
 */
export async function fetchReportData(id: string, token: string): Promise<ReportData | null> {
  const origin = (process.env.API_ORIGIN || process.env.APP_BASE_URL || "").replace(/\/+$/, "");
  if (!origin || !isReportId(id)) return null;
  const response = await fetch(`${origin}/api/v1/reports/${id}/data`, {
    headers: { [REPORT_TOKEN_HEADER]: token, accept: "application/json" },
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(20_000),
  });
  if (!response.ok) return null;
  return (await response.json()) as ReportData;
}
