import type { Metadata } from "next";

import { SecondFactor } from "@/components/auth/second-factor";

export const metadata: Metadata = { title: "Two-step verification" };

export default function MfaPage() {
  return <SecondFactor />;
}
