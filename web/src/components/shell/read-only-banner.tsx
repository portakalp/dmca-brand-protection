"use client";

import { Lock } from "lucide-react";

import { Note } from "@/components/states";
import { readOnlyReason } from "@/lib/access";

import { usePanel } from "./panel";

/** Why the write controls are off (§17): a suspended account. */
export function ReadOnlyBanner() {
  const { me } = usePanel();
  const reason = readOnlyReason(me.customer);
  if (!reason) return null;
  return (
    <Note tone="warning" icon={Lock} role="status">
      {reason}
    </Note>
  );
}
