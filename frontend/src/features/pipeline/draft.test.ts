import { describe, expect, it } from "vitest";

import { makeOpportunity } from "@/test/pipeline-fixtures";

import {
  changedFields,
  createRequest,
  draftFromOpportunity,
  EMPTY_DRAFT,
  mergeConflict,
  updateRequest,
  validateDraft,
} from "./draft";

const LEAD = "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c";

describe("creating", () => {
  it("sends amounts as exact decimal strings and omits empty fields", () => {
    const body = createRequest(
      { ...EMPTY_DRAFT, title: "  Lab upgrade ", value: "12,50,000.5" },
      { lead: LEAD, stage: "s1", stageProbability: "50.00" },
    );
    expect(body).toEqual({ lead: LEAD, title: "Lab upgrade", value: "1250000.5", stage: "s1" });
  });

  it("sends a probability only when it differs from the stage's default", () => {
    const same = createRequest({ ...EMPTY_DRAFT, title: "x", value: "1", probability: "50" }, { lead: LEAD, stageProbability: "50.00" });
    expect(same).not.toHaveProperty("probability");
    const override = createRequest({ ...EMPTY_DRAFT, title: "x", value: "1", probability: "62.5" }, { lead: LEAD, stageProbability: "50.00" });
    expect(override.probability).toBe("62.5");
  });

  it("sends a lost reason only for a lost stage", () => {
    const draft = { ...EMPTY_DRAFT, title: "x", value: "1", lost_reason: "Budget" };
    expect(createRequest(draft, { lead: LEAD })).not.toHaveProperty("lost_reason");
    expect(createRequest(draft, { lead: LEAD, lost: true }).lost_reason).toBe("Budget");
  });
});

describe("validating", () => {
  it("reports every problem", () => {
    expect(validateDraft({ ...EMPTY_DRAFT, value: "1e6", probability: "101", expected_close_date: "1999-01-01" }, { requireLead: true })).toEqual({
      lead: ["Choose the lead this opportunity is for."],
      title: ["Enter a title."],
      value: [expect.stringContaining("at most 2 decimal places")],
      probability: [expect.stringContaining("0 to 100")],
      expected_close_date: ["Enter a date between 2000 and 2099."],
    });
  });

  it("accepts a complete draft", () => {
    expect(validateDraft({ ...EMPTY_DRAFT, title: "x", value: "0" })).toEqual({});
  });
});

describe("editing", () => {
  const opportunity = makeOpportunity({ value: "1250000.00", probability: "62.50", probability_overridden: true });

  it("starts from the opportunity, showing an override but not a stage default", () => {
    expect(draftFromOpportunity(opportunity)).toMatchObject({ value: "1250000", probability: "62.5" });
    expect(draftFromOpportunity(makeOpportunity({ probability_overridden: false })).probability).toBe("");
  });

  it("sends only what changed, with the version", () => {
    const base = draftFromOpportunity(opportunity);
    expect(updateRequest(base, { ...base, value: "12,50,000" }, 3)).toEqual({ version: 3 }); // same amount
    expect(updateRequest(base, { ...base, value: "1300000" }, 3)).toEqual({ version: 3, value: "1300000" });
    // Clearing the expected close date is a change: sent as null.
    expect(updateRequest(base, { ...base, expected_close_date: "" }, 3)).toEqual({ version: 3, expected_close_date: null });
    expect(updateRequest(base, { ...base, probability: "" }, 3)).toEqual({ version: 3, probability: null });
    expect(changedFields(base, { ...base, title: `${base.title}  ` })).toEqual([]);
  });

  it("re-applies my changes on top of someone else's and flags overlaps", () => {
    const base = draftFromOpportunity(opportunity);
    const mine = { ...base, value: "1500000", title: "Mine" };
    const latest = { ...base, value: "1400000", description: "Theirs" };
    const { merged, overlapping } = mergeConflict(base, mine, latest);
    expect(merged).toMatchObject({ value: "1500000", title: "Mine", description: "Theirs" });
    expect(overlapping).toEqual(["value"]);
  });
});
