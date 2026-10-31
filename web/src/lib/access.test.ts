import { describe, expect, it } from "vitest";

import { accessOf, readOnlyReason } from "./access";
import type { Schemas } from "./api/client";

function me(role: "owner" | "viewer", customer: Partial<Schemas["CustomerOut"]> = {}) {
  const writes = role === "owner" && !customer.read_only;
  return {
    user: { id: "u", email: "a@b.example", display_name: null, role, totp_enabled: true },
    customer: {
      id: "c",
      name: "Mintera",
      status: "active",
      is_demo: false,
      read_only: false,
      timezone: "UTC",
      alert_email: null,
      ...customer,
    },
    brands: [],
    permissions: {
      write_whitelist: writes,
      mark_ours: writes,
      manage_users: writes,
      settings: writes,
    },
  } as Schemas["Me"];
}

describe("access", () => {
  it("hides writes from viewers and disables them in a read-only workspace", () => {
    expect(accessOf(me("viewer")).markOurs).toBe("hidden");
    expect(accessOf(me("owner")).whitelist).toBe("enabled");
    const suspended = accessOf(me("owner", { status: "suspended", read_only: true }));
    expect([suspended.markOurs, suspended.whitelist, suspended.users, suspended.settings]).toEqual([
      "disabled",
      "disabled",
      "disabled",
      "disabled",
    ]);
    expect(accessOf(me("viewer", { is_demo: true, read_only: true })).whitelist).toBe("hidden");
  });

  it("explains a read-only account but leaves the demo to its sidebar card", () => {
    const customer = (extra: Partial<Schemas["CustomerOut"]>) => me("owner", extra).customer;
    expect(readOnlyReason(customer({}))).toBeNull();
    expect(readOnlyReason(customer({ is_demo: true, read_only: true }))).toBeNull();
    expect(readOnlyReason(customer({ status: "suspended", read_only: true }))).toMatch(/suspended/);
  });
});
