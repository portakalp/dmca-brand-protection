import type { Metadata } from "next";

import { WhitelistView } from "@/components/whitelist/whitelist-view";

export const metadata: Metadata = { title: "Whitelist" };

export default function WhitelistPage() {
  return <WhitelistView />;
}
