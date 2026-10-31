import type { Metadata } from "next";

import { AcceptInvitation } from "@/components/auth/accept-invitation";

export const metadata: Metadata = { title: "Join your team" };

export default async function InvitePage({ params }: PageProps<"/invite/[token]">) {
  const { token } = await params;
  return <AcceptInvitation token={decodeURIComponent(token)} />;
}
