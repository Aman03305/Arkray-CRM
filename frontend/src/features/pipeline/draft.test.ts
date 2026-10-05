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

const OWNER = "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c";
const COMPLETE = { ...EMPTY_DRAFT, title: "x", value: "1", opportunity_date: "2026-10-01", account_name: "City Lab", customer_name: "Dr. Iyer" };

describe("creating", () => {
  it("sends amounts as exact decimal strings and omits empty fields", () => {
    const body = createRequest(
      { ...COMPLETE, title: "  Lab upgrade ", value: "12,50,000.5", account_name: " City Lab ", customer_name: "Dr. Iyer  " },
      { stage: "s1", stageProbability: "50.00" },
    );
    // The customer details are the customer (ADR-0027): no lead, and no owner in a user's workspace.
    expect(body).toEqual({
      title: "Lab upgrade",
      value: "1250000.5",
      stage: "s1",
      opportunity_date: "2026-10-01",
      account_name: "City Lab",
      customer_name: "Dr. Iyer",
    });
  });

  it("sends the owner only when one is chosen (organisation-wide)", () => {
    expect(createRequest(COMPLETE, {})).not.toHaveProperty("owner");
    expect(createRequest(COMPLETE, { owner: "" })).not.toHaveProperty("owner");
    expect(createRequest(COMPLETE, { owner: OWNER }).owner).toBe(OWNER);
  });

  it("sends the deal's details, the negotiated price and only non-empty custom values", () => {
    const body = createRequest(
      { ...COMPLETE, value: "1", contact_phone: " +91 98765 43210 ", address: "Line 1\nLine 2", work_load: "300 tests/day" },
      { negotiatedPrice: "950000", customFields: { f1: "GEM/1", f2: "", f3: [], f4: false } },
    );
    expect(body).toMatchObject({
      contact_phone: "+91 98765 43210",
      address: "Line 1\nLine 2",
      work_load: "300 tests/day",
      negotiated_price: "950000",
      custom_fields: { f1: "GEM/1", f4: false },
    });
  });

  it("sends a probability only when it differs from the stage's default", () => {
    const same = createRequest({ ...COMPLETE, probability: "50" }, { stageProbability: "50.00" });
    expect(same).not.toHaveProperty("probability");
    const override = createRequest({ ...COMPLETE, probability: "62.5" }, { stageProbability: "50.00" });
    expect(override.probability).toBe("62.5");
  });

  it("sends a lost reason only for a lost stage", () => {
    const draft = { ...COMPLETE, lost_reason: "Budget" };
    expect(createRequest(draft, {})).not.toHaveProperty("lost_reason");
    expect(createRequest(draft, { lost: true }).lost_reason).toBe("Budget");
  });
});

describe("validating", () => {
  it("reports every problem", () => {
    expect(
      validateDraft(
        { ...EMPTY_DRAFT, value: "1e6", probability: "101", expected_close_date: "1999-01-01", contact_email: "nope" },
        { requireOwner: true },
      ),
    ).toEqual({
      owner: ["Choose who owns this opportunity."],
      title: ["Enter a name."],
      account_name: ["Enter the account name."],
      customer_name: ["Enter the customer name."],
      opportunity_date: ["Enter a date between 2000 and 2099."],
      value: [expect.stringContaining("at most 2 decimal places")],
      probability: [expect.stringContaining("0 to 100")],
      expected_close_date: ["Enter a date between 2000 and 2099."],
      contact_email: ["Enter a valid email address."],
    });
  });

  it("accepts a complete draft", () => {
    expect(validateDraft({ ...COMPLETE, value: "0" })).toEqual({});
    // Organisation-wide, the owner is required; once chosen the draft is complete.
    expect(validateDraft(COMPLETE, { requireOwner: true, owner: OWNER })).toEqual({});
  });

  it("requires the customer: account and customer name, not just spaces", () => {
    expect(validateDraft({ ...COMPLETE, account_name: "  ", customer_name: "" })).toEqual({
      account_name: ["Enter the account name."],
      customer_name: ["Enter the customer name."],
    });
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
