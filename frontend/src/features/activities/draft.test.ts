import { describe, expect, it } from "vitest";

import { makeActivity, makeMeeting } from "@/test/activity-fixtures";

import {
  changedFields,
  createRequest,
  DEFAULT_DUE_TIME,
  draftFromActivity,
  EMPTY_DRAFT,
  endAfterStartChange,
  mergeConflict,
  suggestedEnd,
  updateRequest,
  validateDraft,
} from "./draft";

describe("task drafts", () => {
  it("send the due time typed in India time as a UTC instant, for the opportunity (never a lead)", () => {
    const body = createRequest("task", { ...EMPTY_DRAFT, title: "  Call   back ", dueDate: "2026-10-03", dueTime: "00:15" }, "O");
    expect(body).toEqual({ type: "task", title: "Call back", opportunity: "O", priority: "normal", due_at: "2026-10-02T18:45:00.000Z" });
    expect(body).not.toHaveProperty("lead");
  });

  it("default the due time to the end of the working day and send no due date when empty", () => {
    expect(EMPTY_DRAFT.dueTime).toBe(DEFAULT_DUE_TIME);
    expect(createRequest("task", { ...EMPTY_DRAFT, title: "x", dueDate: "2026-10-03", dueTime: "" }, "O").due_at).toBe(
      "2026-10-03T12:30:00.000Z",
    );
    expect(createRequest("task", { ...EMPTY_DRAFT, title: "x" }, "O")).not.toHaveProperty("due_at");
  });

  it("round-trip an activity's due time in India time", () => {
    const draft = draftFromActivity(makeActivity({ due_at: "2026-10-02T18:45:00Z" }));
    expect([draft.dueDate, draft.dueTime]).toEqual(["2026-10-03", "00:15"]);
  });

  it("send only what changed, with the version the edit started from", () => {
    const base = draftFromActivity(makeActivity());
    expect(updateRequest("task", base, { ...base, title: "  Renamed  ", priority: "high" }, 3)).toEqual({
      version: 3,
      title: "Renamed",
      priority: "high",
    });
    expect(updateRequest("task", base, { ...base, dueDate: "" }, 3)).toEqual({ version: 3, due_at: null });
    expect(changedFields("task", base, { ...base, title: `${base.title}  ` })).toEqual([]);
  });

  it("validate before sending", () => {
    expect(validateDraft("task", EMPTY_DRAFT, { requireOpportunity: true })).toEqual({
      opportunity: ["Choose the opportunity this is about."],
      title: ["Enter a subject."],
    });
    expect(validateDraft("task", { ...EMPTY_DRAFT, title: "x" }, { requireOpportunity: true, opportunity: "O" })).toEqual({});
    // An edit (or a form opened from a deal) doesn't ask for one.
    expect(validateDraft("task", { ...EMPTY_DRAFT, title: "x" })).toEqual({});
    expect(validateDraft("task", { ...EMPTY_DRAFT, title: "x", dueDate: "1999-12-31" }).due_at).toBeDefined();
  });
});

describe("meeting drafts", () => {
  it("send start and end as UTC instants and only filled optional fields", () => {
    const draft = { ...EMPTY_DRAFT, title: "Demo", startsAt: "2026-10-06T11:00", endsAt: "2026-10-06T12:00", location: " Andheri " };
    expect(createRequest("meeting", draft, "O")).toEqual({
      type: "meeting",
      title: "Demo",
      opportunity: "O",
      starts_at: "2026-10-06T05:30:00.000Z",
      ends_at: "2026-10-06T06:30:00.000Z",
      location: "Andheri",
    });
  });

  it("suggest an end 30 minutes after the start", () => {
    expect(suggestedEnd("2026-10-06T23:45")).toBe("2026-10-07T00:15");
    expect(suggestedEnd("")).toBe("");
  });

  it("keep a meeting's length when its start moves (review: the end stayed behind)", () => {
    expect(endAfterStartChange("2026-10-06T11:00", "2026-10-06T12:00", "2026-10-06T15:00")).toBe("2026-10-06T16:00");
    expect(endAfterStartChange("2026-10-06T23:30", "2026-10-07T00:15", "2026-10-07T09:00")).toBe("2026-10-07T09:45");
    // No valid length to keep: an empty end is suggested, a typed one is left alone.
    expect(endAfterStartChange("", "", "2026-10-06T15:00")).toBe("2026-10-06T15:30");
    expect(endAfterStartChange("", "2026-10-06T18:00", "2026-10-06T15:00")).toBe("2026-10-06T18:00");
    expect(endAfterStartChange("2026-10-06T12:00", "2026-10-06T11:00", "2026-10-06T15:00")).toBe("2026-10-06T11:00");
  });

  it("refuse inverted, over-long and unsafe-link meetings", () => {
    const base = { ...EMPTY_DRAFT, title: "x", startsAt: "2026-10-06T11:00" };
    expect(validateDraft("meeting", { ...base, endsAt: "2026-10-06T10:00" }).ends_at).toEqual(["The end must be after the start."]);
    expect(validateDraft("meeting", { ...base, endsAt: "2026-10-07T11:01" }).ends_at).toEqual(["A meeting can last at most 24 hours."]);
    expect(validateDraft("meeting", { ...base, endsAt: "2026-10-06T12:00", meetingUrl: "javascript:alert(1)" }).meeting_url).toBeDefined();
    expect(validateDraft("meeting", { ...base, endsAt: "2026-10-06T12:00", meetingUrl: "http://meet.example" }).meeting_url).toBeDefined();
    expect(validateDraft("meeting", { ...base, endsAt: "2026-10-06T12:00", meetingUrl: "https://meet.example/x" })).toEqual({});
  });
});

describe("edit conflicts", () => {
  it("re-apply my changes onto the latest version and flag fields we both changed", () => {
    const base = draftFromActivity(makeMeeting());
    const mine = { ...base, title: "Mine", location: "Office" };
    const latest = { ...base, title: "Theirs", description: "Their agenda" };
    const { merged, overlapping } = mergeConflict("meeting", base, mine, latest);
    expect(merged).toMatchObject({ title: "Mine", location: "Office", description: "Their agenda" });
    expect(overlapping).toEqual(["title"]);
  });
});
