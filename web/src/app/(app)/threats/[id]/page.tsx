import type { Metadata } from "next";

import { ThreatDetailView } from "@/components/threat-detail/threat-detail-view";

export const metadata: Metadata = { title: "Threat" };

export default async function ThreatPage({ params }: PageProps<"/threats/[id]">) {
  const { id } = await params;
  return <ThreatDetailView id={id} />;
}
