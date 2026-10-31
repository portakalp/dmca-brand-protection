"use client";

import { Download } from "lucide-react";

import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { SimpleTable, Td, Th, Tr } from "@/components/simple-table";
import { Card, EmptyState, ErrorState, LoadingState, PanelHeader } from "@/components/states";
import { Indicator } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import { useReports } from "@/lib/api/queries";
import { longDay, todayIn, zoneName } from "@/lib/period";
import { reportMonth } from "@/lib/report";

function FileLink({ report, format }: { report: Schemas["ReportItem"]; format: "pdf" | "csv" }) {
  const label = format.toUpperCase();
  return (
    <Button asChild variant="outline" size="sm">
      <a
        href={`/api/v1/reports/${report.id}/download?format=${format}`}
        download
        aria-label={`Download the ${reportMonth(report.period)} report as ${label}`}
      >
        <Download aria-hidden="true" />
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
      <Card aria-labelledby="reports-title" className="flex flex-col overflow-hidden">
        <PanelHeader
          id="reports-title"
          title="Monthly reports"
          description={`One per month, ready early the next month. Times are in ${zoneName(timeZone)}. The PDF is for sharing; the CSV has one row per threat.`}
          className="pb-4"
        />
        {reports.isPending ? (
          <LoadingState rows={3} label="Loading reports" className="border-t border-border" />
        ) : reports.isError ? (
          <ErrorState
            error={reports.error}
            onRetry={() => reports.refetch()}
            className="border-t border-border"
          />
        ) : reports.data.length === 0 ? (
          <EmptyState title="No reports yet" className="border-t border-border">
            Your first monthly report appears here at the start of next month.
          </EmptyState>
        ) : (
          <SimpleTable caption="Monthly reports" minWidth={480}>
            <thead>
              <tr>
                <Th>Month</Th>
                <Th>Ready</Th>
                <Th className="text-right">
                  <span className="sr-only">Downloads</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {reports.data.map((report) => (
                <Tr key={report.id}>
                  <Td primary className="font-medium text-text">
                    {reportMonth(report.period)}
                  </Td>
                  <Td label="Ready" className="tabular whitespace-nowrap">
                    {report.generated_at ? (
                      longDay(todayIn(timeZone, new Date(report.generated_at)))
                    ) : (
                      <Indicator tone="warning">Being prepared</Indicator>
                    )}
                  </Td>
                  <Td action className="py-1.5 text-right whitespace-nowrap">
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
