import { describe, expect, it } from "vitest";

import { asListItem, makeActivity, makeMeeting } from "@/test/activity-fixtures";

import {
  businessDateOf,
  businessMinutesOf,
  byDay,
  clockLabel,
  layoutDay,
  monthWeeks,
  periodTitle,
  scheduleFor,
  shiftAnchor,
  visibleDays,
} from "./calendar";

const task = (id: string, due_at: string) => asListItem(makeActivity({ id, title: id, due_at }));
const meeting = (id: string, starts_at: string, ends_at: string) => asListItem(makeMeeting({ id, title: id, starts_at, ends_at }));

describe("the calendar's days", () => {
  it("a month runs from the Monday of its first week to the Sunday of its last", () => {
    const october = monthWeeks("2026-10-15");
    expect(october).toHaveLength(5);
    expect(october[0]![0]).toBe("2026-09-28");
    expect(october.at(-1)![6]).toBe("2026-11-01");
    // August 2026 starts on a Saturday and ends on a Monday: six weeks.
    expect(monthWeeks("2026-08-01")).toHaveLength(6);
    // February 2027 starts on a Monday and ends on a Sunday: exactly four.
    expect(monthWeeks("2027-02-10").flat()).toEqual(visibleDays("month", "2027-02-01"));
    expect(monthWeeks("2027-02-10")).toHaveLength(4);
  });

  it("a week is Monday to Sunday; a day is itself", () => {
    expect(visibleDays("week", "2026-10-01")).toEqual(["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]);
    expect(visibleDays("week", "2026-10-04")[0]).toBe("2026-09-28"); // a Sunday stays in its week
    expect(visibleDays("day", "2026-10-05")).toEqual(["2026-10-05"]);
  });

  it("previous and next move by the view's period, without month-end overflow", () => {
    expect(shiftAnchor("month", "2026-01-31", 1)).toBe("2026-02-01");
    expect(shiftAnchor("month", "2026-12-15", 1)).toBe("2027-01-01");
    expect(shiftAnchor("month", "2026-03-31", -1)).toBe("2026-02-01");
    expect(shiftAnchor("week", "2026-12-29", 1)).toBe("2027-01-05");
    expect(shiftAnchor("day", "2026-03-01", -1)).toBe("2026-02-28");
  });

  it("titles name the period", () => {
    expect(periodTitle("month", "2026-10-05")).toBe("October 2026");
    expect(periodTitle("week", "2026-10-07")).toBe("5 – 11 Oct 2026");
    expect(periodTitle("week", "2026-10-01")).toBe("28 Sep – 4 Oct 2026");
    expect(periodTitle("week", "2026-12-31")).toBe("28 Dec 2026 – 3 Jan 2027");
    expect(periodTitle("day", "2026-10-05")).toBe("Monday, 5 October 2026");
  });
});

describe("times on the calendar are India time, whatever the browser's zone", () => {
  it("an instant falls on its business date, at its business time", () => {
    // 18:45 UTC on 1 October is 00:15 on 2 October in India.
    expect(businessDateOf("2026-10-01T18:45:00Z")).toBe("2026-10-02");
    expect(businessMinutesOf("2026-10-01T18:45:00Z")).toBe(15);
    expect(businessDateOf(null)).toBe("");
  });

  it("clock labels are 12-hour", () => {
    expect(clockLabel(19 * 60)).toBe("7:00 pm");
    expect(clockLabel(0)).toBe("12:00 am");
    expect(clockLabel(12 * 60 + 30)).toBe("12:30 pm");
    expect(clockLabel(9 * 60 + 5)).toBe("9:05 am");
  });

  it("entries are grouped by business day, each day's in time order", () => {
    const days = byDay([
      task("late", "2026-10-05T12:30:00Z"), // 18:00 IST, 5 Oct
      meeting("early", "2026-10-05T03:30:00Z", "2026-10-05T04:00:00Z"), // 09:00 IST, 5 Oct
      task("next-day", "2026-10-05T18:40:00Z"), // 00:10 IST, 6 Oct
    ]);
    expect(days.get("2026-10-05")!.map((a) => a.id)).toEqual(["early", "late"]);
    expect(days.get("2026-10-06")!.map((a) => a.id)).toEqual(["next-day"]);
  });
});

describe("a day's entries on the hour grid", () => {
  it("overlapping entries share the width; later ones get it back", () => {
    const placed = layoutDay([
      meeting("a", "2026-10-05T04:30:00Z", "2026-10-05T05:30:00Z"), // 10:00-11:00
      meeting("b", "2026-10-05T05:00:00Z", "2026-10-05T06:00:00Z"), // 10:30-11:30
      task("c", "2026-10-05T07:30:00Z"), // 13:00, 30 min
    ]);
    const by = Object.fromEntries(placed.map((p) => [p.item.id, p]));
    expect(by.a).toMatchObject({ start: 600, length: 60, lane: 0, lanes: 2 });
    expect(by.b).toMatchObject({ start: 630, length: 60, lane: 1, lanes: 2 });
    expect(by.c).toMatchObject({ start: 780, length: 30, lane: 0, lanes: 1 });
  });

  it("a freed lane is reused within a cluster", () => {
    const placed = layoutDay([
      meeting("long", "2026-10-05T04:30:00Z", "2026-10-05T07:30:00Z"), // 10:00-13:00
      meeting("first", "2026-10-05T04:30:00Z", "2026-10-05T05:30:00Z"), // 10:00-11:00
      meeting("second", "2026-10-05T05:30:00Z", "2026-10-05T06:30:00Z"), // 11:00-12:00
    ]);
    const by = Object.fromEntries(placed.map((p) => [p.item.id, p]));
    expect([by.long!.lane, by.first!.lane, by.second!.lane]).toEqual([0, 1, 1]);
    expect(placed.every((p) => p.lanes === 2)).toBe(true);
  });

  it("a meeting running past midnight is cut at the end of its day", () => {
    const [block] = layoutDay([meeting("late", "2026-10-05T17:30:00Z", "2026-10-05T19:30:00Z")]); // 23:00-01:00
    expect(block).toMatchObject({ start: 23 * 60, length: 60 });
  });
});

describe("what the form starts with for a chosen day or time", () => {
  it("a whole day: a task due at the end of the working day, a meeting at 9:00 for 30 minutes", () => {
    expect(scheduleFor("2026-10-07")).toEqual({ dueDate: "2026-10-07", dueTime: "18:00", startsAt: "2026-10-07T09:00", endsAt: "2026-10-07T09:30" });
  });

  it("a time: both start then; a late meeting ends the next day", () => {
    expect(scheduleFor("2026-10-07", 14 * 60 + 30)).toEqual({ dueDate: "2026-10-07", dueTime: "14:30", startsAt: "2026-10-07T14:30", endsAt: "2026-10-07T15:00" });
    expect(scheduleFor("2026-10-07", 23 * 60 + 45).endsAt).toBe("2026-10-08T00:15");
  });
});
