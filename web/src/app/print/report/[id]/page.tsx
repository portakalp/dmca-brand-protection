import type { Metadata } from "next";
import { headers } from "next/headers";
import { Suspense } from "react";

import { ReportDocument } from "@/components/report/report-document";
import { fetchReportData } from "@/lib/api/report-data";
import { REPORT_TOKEN_HEADER } from "@/lib/report";

export const metadata: Metadata = { title: "Monthly report" };

/**
 * The monthly report as worker-browser prints it (§18, §21). The proxy lets this route through
 * only with X-Report-Token; the data is read here on the server with that token, which the
 * page never renders. Without data the page says so and is never marked ready, so the print
 * fails and is tried again with a new token.
 */
export default function PrintReportPage({ params }: PageProps<"/print/report/[id]">) {
  return (
    <main className="bg-surface py-2">
      <Suspense fallback={null}>
        <Report params={params} />
      </Suspense>
    </main>
  );
}

async function Report({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const token = (await headers()).get(REPORT_TOKEN_HEADER);
  const data = token ? await fetchReportData(id, token).catch(() => null) : null;
  if (data === null) {
    return <p className="p-8 text-[14px] text-text-2">This report is not available.</p>;
  }
  return <ReportDocument data={data} />;
}
