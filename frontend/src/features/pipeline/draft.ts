/**
 * The opportunity form's editable state and its conversions to API requests. Pure
 * functions, unit-tested without rendering: what is sent, what counts as a change, how an
 * edit conflict is merged. Amounts and percentages stay decimal strings throughout.
 */
import type { Opportunity, OpportunityCreateRequest, OpportunityUpdateRequest } from "@/lib/api/types";
import { amountInputValue, parseAmountInput, parsePercentInput, sameDecimal } from "@/lib/money";

export const DRAFT_FIELDS = ["title", "value", "probability", "expected_close_date", "description", "lost_reason"] as const;
export type DraftField = (typeof DRAFT_FIELDS)[number];

/**
 * Every value as the form holds it (text). `probability` is "" for "the stage's default";
 * `expected_close_date` is "" for none.
 */
export type Draft = Record<DraftField, string>;

export const FIELD_LABELS: Record<DraftField, string> = {
  title: "Title",
  value: "Value",
  probability: "Probability",
  expected_close_date: "Expected close date",
  description: "Description",
  lost_reason: "Lost reason",
};

export const EMPTY_DRAFT: Draft = { title: "", value: "", probability: "", expected_close_date: "", description: "", lost_reason: "" };

export function draftFromOpportunity(opportunity: Opportunity): Draft {
  return {
    title: opportunity.title,
    value: amountInputValue(opportunity.value),
    probability: opportunity.probability_overridden ? amountInputValue(opportunity.probability) : "",
    expected_close_date: opportunity.expected_close_date ?? "",
    description: opportunity.description,
    lost_reason: opportunity.lost_reason,
  };
}

function comparable(field: DraftField, value: string): string {
  const text = value.trim();
  if (field === "value") {
    const parsed = parseAmountInput(text);
    return parsed.ok ? amountInputValue(parsed.value) : text;
  }
  if (field === "probability") {
    const parsed = parsePercentInput(text);
    return parsed.ok ? amountInputValue(parsed.value) : text;
  }
  return field === "description" || field === "lost_reason" ? text : text.replace(/\s+/g, " ");
}

export function changedFields(before: Draft, after: Draft): DraftField[] {
  return DRAFT_FIELDS.filter((f) => comparable(f, before[f]) !== comparable(f, after[f]));
}

export type Problems = Partial<Record<DraftField | "lead" | "stage", string[]>>;

/** Client-side checks (the server re-checks everything); each field's first problem. */
export function validateDraft(draft: Draft, { requireLead = false, lead = "" } = {}): Problems {
  const problems: Problems = {};
  if (requireLead && !lead) problems.lead = ["Choose the lead this opportunity is for."];
  if (!draft.title.trim()) problems.title = ["Enter a title."];
  const value = parseAmountInput(draft.value);
  if (!value.ok) problems.value = [value.error];
  if (draft.probability.trim()) {
    const probability = parsePercentInput(draft.probability);
    if (!probability.ok) problems.probability = [probability.error];
  }
  const date = draft.expected_close_date;
  if (date && !(/^\d{4}-\d{2}-\d{2}$/.test(date) && date >= "2000-01-01" && date <= "2099-12-31")) {
    problems.expected_close_date = ["Enter a date between 2000 and 2099."];
  }
  return problems;
}

function amount(text: string): string {
  const parsed = parseAmountInput(text);
  if (!parsed.ok) throw new Error(parsed.error); // validateDraft runs first
  return parsed.value;
}

function percent(text: string): string | null {
  if (!text.trim()) return null;
  const parsed = parsePercentInput(text);
  if (!parsed.ok) throw new Error(parsed.error);
  return parsed.value;
}

export function createRequest(
  draft: Draft,
  target: { lead: string; pipeline?: string; stage?: string; stageProbability?: string; lost?: boolean },
): OpportunityCreateRequest {
  const body: { -readonly [K in keyof OpportunityCreateRequest]: OpportunityCreateRequest[K] } = {
    lead: target.lead,
    title: draft.title.trim(),
    value: amount(draft.value),
  };
  if (target.pipeline) body.pipeline = target.pipeline;
  if (target.stage) body.stage = target.stage;
  const probability = percent(draft.probability);
  if (probability !== null && !sameDecimal(probability, target.stageProbability)) body.probability = probability;
  if (draft.expected_close_date) body.expected_close_date = draft.expected_close_date;
  if (draft.description.trim()) body.description = draft.description;
  if (target.lost && draft.lost_reason.trim()) body.lost_reason = draft.lost_reason.trim();
  return body;
}

/** Only what changed, plus the version the edit started from. */
export function updateRequest(base: Draft, draft: Draft, version: number): OpportunityUpdateRequest {
  const body: { -readonly [K in keyof OpportunityUpdateRequest]: OpportunityUpdateRequest[K] } = { version };
  for (const field of changedFields(base, draft)) {
    switch (field) {
      case "value":
        body.value = amount(draft.value);
        break;
      case "probability":
        body.probability = percent(draft.probability); // null: back to the stage's default
        break;
      case "expected_close_date":
        body.expected_close_date = draft.expected_close_date || null;
        break;
      case "title":
        body.title = draft.title.trim();
        break;
      default:
        body[field] = draft[field];
    }
  }
  return body;
}

/**
 * An edit conflict (409): someone saved the opportunity after this form loaded it. Re-apply
 * this user's own changes on top of the latest version; report fields both people changed
 * to different values, so the user reviews them before saving again.
 */
export function mergeConflict(base: Draft, mine: Draft, latest: Draft): { merged: Draft; overlapping: DraftField[] } {
  const myChanges = changedFields(base, mine);
  const theirChanges = new Set(changedFields(base, latest));
  const merged = { ...latest };
  for (const field of myChanges) merged[field] = mine[field];
  const overlapping = myChanges.filter((f) => theirChanges.has(f) && comparable(f, latest[f]) !== comparable(f, mine[f]));
  return { merged, overlapping };
}
