import { describe, expect, it } from "vitest";

import { isReportId, previousMonthName, reportMoment, reportMonth, takedownTime } from "./report";

describe("report", () => {
  it("names the month and the one before", () => {
    expect(reportMonth("2026-10")).toBe("October 2026");
    expect(reportMonth("2027-01")).toBe("January 2027");
    expect(previousMonthName({ start: "2026-09-01", end: "2026-09-30" })).toBe("September");
  });

  it("writes takedown moments in the customer's time zone", () => {
    expect(reportMoment("2026-10-12T11:00:00Z", "Europe/Warsaw")).toBe("Oct 12, 13:00");
    expect(reportMoment(null, "Europe/Warsaw")).toBe("—");
    expect(takedownTime(30.5)).toBe("30.5 h");
    expect(takedownTime(2)).toBe("2.0 h");
    expect(takedownTime(null)).toBe("—");
  });

  it("only takes a report id as the print page's id", () => {
    expect(isReportId("4f9d6f0e-5d41-4c55-9f1d-8a6f1f9b2c11")).toBe(true);
    expect(isReportId("../../api/v1/me")).toBe(false);
    expect(isReportId("4f9d6f0e-5d41-4c55-9f1d-8a6f1f9b2c11/x")).toBe(false);
  });
});
