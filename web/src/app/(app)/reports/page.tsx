import type { Metadata } from "next";

import { ReportsView } from "@/components/account/reports-view";

export const metadata: Metadata = { title: "Reports" };

export default function ReportsPage() {
  return <ReportsView />;
}
