import type { Metadata } from "next";

import { ThreatsView } from "@/components/threats/threats-view";

export const metadata: Metadata = { title: "Threats" };

export default function ThreatsPage() {
  return <ThreatsView />;
}
