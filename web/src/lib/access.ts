import type { Schemas } from "@/lib/api/client";

/**
 * How a write control shows (architecture.md §17, §18):
 * - "hidden" for viewers, who only read;
 * - "disabled" for owners of a read-only workspace (the demo, a suspended account), next to the
 *   banner that explains why;
 * - "enabled" otherwise. The API refuses the same writes either way.
 */
export type WriteMode = "hidden" | "disabled" | "enabled";

export type Access = {
  owner: boolean;
  readOnly: boolean;
  markOurs: WriteMode;
  whitelist: WriteMode;
  users: WriteMode;
  settings: WriteMode;
};

function mode(me: Schemas["Me"], allowed: boolean): WriteMode {
  if (me.user.role !== "owner") return "hidden";
  if (me.customer.read_only) return "disabled";
  return allowed ? "enabled" : "hidden";
}

export function accessOf(me: Schemas["Me"]): Access {
  return {
    owner: me.user.role === "owner",
    readOnly: me.customer.read_only,
    markOurs: mode(me, me.permissions.mark_ours),
    whitelist: mode(me, me.permissions.write_whitelist),
    users: mode(me, me.permissions.manage_users),
    settings: mode(me, me.permissions.settings),
  };
}

/**
 * The read-only banner's text; null when the workspace can write. The demo has none: its
 * sidebar card already says it is sample data, as in the design.
 */
export function readOnlyReason(customer: Schemas["CustomerOut"]): string | null {
  if (!customer.read_only || customer.is_demo) return null;
  return "This account is suspended. You can still read everything, but changes are turned off.";
}
