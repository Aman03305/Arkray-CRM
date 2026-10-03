import { describe, expect, it } from "vitest";

import {
  businessToday,
  formatDate,
  formatDateOnly,
  formatDateTime,
  formatRelative,
  fromBusinessDateTimeInput,
  toBusinessDateTimeInput,
} from "./format";

describe("business time zone formatting", () => {
  it("shows dates in Asia/Kolkata, not the browser's zone", () => {
    // 20:00 UTC on 30 Sep is 01:30 on 1 Oct in India.
    expect(formatDate("2026-09-30T20:00:00Z")).toBe("1 Oct 2026");
    expect(formatDateTime("2026-09-30T20:00:00Z")).toMatch(/1 Oct 2026.*01:30.*(IST|GMT\+5:30)/i);
  });

  it("falls back for missing or invalid values", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDateTime("not-a-date", "Never")).toBe("Never");
  });

  it("describes times relative to now", () => {
    const now = new Date("2026-09-30T12:00:00Z");
    expect(formatRelative("2026-09-30T09:00:00Z", now)).toBe("3 hours ago");
    expect(formatRelative("2026-10-03T12:00:00Z", now)).toBe("in 3 days");
    expect(formatRelative("2026-09-30T11:59:30Z", now)).toBe("just now");
  });
});

describe("datetime inputs in the business time zone", () => {
  it("shows an instant as IST wall-clock time, and reads it back as the same instant", () => {
    expect(toBusinessDateTimeInput("2026-09-30T20:00:00Z")).toBe("2026-10-01T01:30");
    expect(fromBusinessDateTimeInput("2026-10-01T01:30")).toBe("2026-09-30T20:00:00.000Z");
  });

  it("round-trips across midnight and the year boundary", () => {
    for (const iso of ["2026-12-31T18:29:00.000Z", "2026-12-31T18:30:00.000Z", "2027-01-01T00:00:00.000Z"]) {
      expect(fromBusinessDateTimeInput(toBusinessDateTimeInput(iso))).toBe(iso);
    }
  });

  it("treats empty or malformed values as no value", () => {
    expect(toBusinessDateTimeInput(null)).toBe("");
    expect(toBusinessDateTimeInput("not a date")).toBe("");
    expect(fromBusinessDateTimeInput("")).toBeNull();
    expect(fromBusinessDateTimeInput("2026-09-30")).toBeNull();
    expect(fromBusinessDateTimeInput("30/09/2026 10:00")).toBeNull();
  });
});

describe("date-only values", () => {
  it("formats a business date from its digits, in any browser time zone", () => {
    expect(formatDateOnly("2026-10-15")).toBe("15 Oct 2026");
    expect(formatDateOnly("2026-01-01")).toBe("1 Jan 2026");
    expect(formatDateOnly("2026-02-30")).toBe("—");
    expect(formatDateOnly("15/10/2026")).toBe("—");
    expect(formatDateOnly(null)).toBe("—");
  });

  it("knows today's business date in India", () => {
    // 20:00 UTC on 30 Sep is already 1 Oct in Asia/Kolkata.
    expect(businessToday(new Date("2026-09-30T20:00:00Z"))).toBe("2026-10-01");
    expect(businessToday(new Date("2026-09-30T18:00:00Z"))).toBe("2026-09-30");
  });
});
