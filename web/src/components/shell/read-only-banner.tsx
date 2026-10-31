"use client";

import { Lock } from "lucide-react";

import { readOnlyReason } from "@/lib/access";

import { usePanel } from "./panel";

/** Why the write controls are off (§17): a suspended account. */
export function ReadOnlyBanner() {
  const { me } = usePanel();
  const reason = readOnlyReason(me.customer);
  if (!reason) return null;
  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-card border border-warning-border bg-warning-bg px-4 py-2.5 text-[13px] text-warning-fg"
    >
      <Lock aria-hidden="true" className="size-4 flex-none text-warning-icon" />
      <p className="min-w-0 flex-[1_1_240px]">{reason}</p>
    </div>
  );
}
