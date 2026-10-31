"use client";

import { Download } from "lucide-react";

import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { SimpleTable, Td, Th, Tr } from "@/components/simple-table";
import { Card, EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { useReports } from "@/lib/api/queries";
import { longDay, todayIn } from "@/lib/period";
import { reportMonth } from "@/lib/report";

function FileLink({ report, format }: { report: Schemas["ReportItem"]; format: "pdf" | "csv" }) {
  const label = format.toUpperCase();
  return (
    <Button asChild variant="control" size="control">
      <a
        href={`/api/v1/reports/${report.id}/download?format=${format}`}
        download
        aria-label={`Download the ${reportMonth(report.period)} report as ${label}`}
        className="text-text no-underline hover:text-text hover:no-underline"
      >
        <Download aria-hidden="true" className="text-text-secondary" />
        {label}
      </a>
    </Button>
  );
}

export function ReportsView() {
  const { timeZone } = usePanel();
  const reports = useReports();
  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="A monthly summary of what was found and taken down, as PDF and CSV."
      />
      <Card aria-label="Reports" className="flex flex-col overflow-hidden">
        {reports.isPending ? (
          <div aria-busy="true" aria-label="Loading reports" className="flex flex-col gap-2 p-5">
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-11" />
            ))}
          </div>
        ) : reports.isError ? (
          <ErrorState error={reports.error} onRetry={() => reports.refetch()} />
        ) : reports.data.length === 0 ? (
          <EmptyState title="No reports yet">
            Your first monthly report appears here at the start of next month.
          </EmptyState>
        ) : (
          <SimpleTable caption="Monthly reports" minWidth={480}>
            <thead>
              <tr>
                <Th>Month</Th>
                <Th>Ready</Th>
                <Th>
                  <span className="sr-only">Downloads</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {reports.data.map((report) => (
                <Tr key={report.id}>
                  <Td className="font-medium text-text">{reportMonth(report.period)}</Td>
                  <Td className="tabular whitespace-nowrap">
                    {report.generated_at
                      ? longDay(todayIn(timeZone, new Date(report.generated_at)))
                      : "Being prepared"}
                  </Td>
                  <Td className="py-1.5 text-right whitespace-nowrap">
                    <span className="inline-flex gap-2">
                      {report.pdf ? <FileLink report={report} format="pdf" /> : null}
                      {report.csv ? <FileLink report={report} format="csv" /> : null}
                    </span>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </SimpleTable>
        )}
      </Card>
    </>
  );
}
