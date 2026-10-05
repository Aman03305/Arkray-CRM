/**
 * The opportunity form's editable state and its conversions to API requests. Pure
 * functions, unit-tested without rendering: what is sent, what counts as a change, how an
 * edit conflict is merged. Amounts and percentages stay decimal strings throughout.
 *
 * There is no name field: the server names an opportunity after its customer and instrument
 * (ADR-0028), and makes its lead from the customer details.
 */
import type { CustomField, Opportunity, OpportunityCreateRequest, OpportunityUpdateRequest } from "@/lib/api/types";
import { amountInputValue, parseAmountInput, parsePercentInput, sameDecimal } from "@/lib/money";

export const DRAFT_FIELDS = [
  "opportunity_date",
  "account_name",
  "customer_name",
  "contact_phone",
  "contact_email",
  "address",
  "instrument_name",
  "work_load",
  "value",
  "expected_cpt",
  "probability",
  "expected_close_date",
  "description",
  "lost_reason",
] as const;
export type DraftField = (typeof DRAFT_FIELDS)[number];

/**
 * Every value as the form holds it (text). `probability` is "" for "the stage's default";
 * dates are "" for none.
 */
export type Draft = Record<DraftField, string>;

export const FIELD_LABELS: Record<DraftField, string> = {
  opportunity_date: "Opportunity date",
  account_name: "Account name",
  customer_name: "Customer name",
  contact_phone: "Contact",
  contact_email: "Email",
  address: "Address",
  instrument_name: "Instrument name",
  work_load: "Work load",
  value: "Installation price",
  expected_cpt: "Expected CPT",
  probability: "Probability",
  expected_close_date: "Expected closing date",
  description: "Description",
  lost_reason: "Lost reason",
};

const MULTILINE: ReadonlySet<DraftField> = new Set(["description", "lost_reason", "address"]);

export const EMPTY_DRAFT: Draft = {
  opportunity_date: "",
  account_name: "",
  customer_name: "",
  contact_phone: "",
  contact_email: "",
  address: "",
  instrument_name: "",
  work_load: "",
  value: "",
  expected_cpt: "",
  probability: "",
  expected_close_date: "",
  description: "",
  lost_reason: "",
};

export function draftFromOpportunity(opportunity: Opportunity): Draft {
  return {
    opportunity_date: opportunity.opportunity_date,
    account_name: opportunity.account_name,
    customer_name: opportunity.customer_name,
    contact_phone: opportunity.contact_phone,
    contact_email: opportunity.contact_email,
    address: opportunity.address,
    instrument_name: opportunity.instrument_name,
    work_load: opportunity.work_load,
    value: amountInputValue(opportunity.value),
    expected_cpt: opportunity.expected_cpt,
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
  return MULTILINE.has(field) ? text : text.replace(/\s+/g, " ");
}

export function changedFields(before: Draft, after: Draft): DraftField[] {
  return DRAFT_FIELDS.filter((f) => comparable(f, before[f]) !== comparable(f, after[f]));
}

export type Problems = Partial<Record<DraftField | "owner" | "pipeline" | "stage" | "negotiated_price" | `custom_fields.${string}`, string[]>>;

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const inRange = (date: string) => DATE.test(date) && date >= "2000-01-01" && date <= "2099-12-31";
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * Client-side checks (the server re-checks everything); each field's first problem. A new
 * opportunity needs the customer or the account name (its lead and its name are made from
 * them); an existing one keeps both (neither can be cleared).
 */
export function validateDraft(draft: Draft, { requireOwner = false, owner = "", creating = false } = {}): Problems {
  const problems: Problems = {};
  if (requireOwner && !owner) problems.owner = ["Choose who owns this opportunity."];
  const account = draft.account_name.trim();
  const customer = draft.customer_name.trim();
  if (creating) {
    if (!account && !customer) problems.customer_name = ["Enter the customer name or the account name."];
  } else {
    if (!account) problems.account_name = ["Enter the account name."];
    if (!customer) problems.customer_name = ["Enter the customer name."];
  }
  if (!draft.opportunity_date || !inRange(draft.opportunity_date)) problems.opportunity_date = ["Enter a date between 2000 and 2099."];
  const value = parseAmountInput(draft.value);
  if (!value.ok) problems.value = [value.error];
  if (draft.probability.trim()) {
    const probability = parsePercentInput(draft.probability);
    if (!probability.ok) problems.probability = [probability.error];
  }
  if (draft.expected_close_date && !inRange(draft.expected_close_date)) {
    problems.expected_close_date = ["Enter a date between 2000 and 2099."];
  }
  if (draft.contact_email.trim() && !EMAIL.test(draft.contact_email.trim())) problems.contact_email = ["Enter a valid email address."];
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

const OPTIONAL_TEXT = ["account_name", "customer_name", "contact_phone", "contact_email", "address", "instrument_name", "work_load", "expected_cpt"] as const;

export function createRequest(
  draft: Draft,
  target: {
    /** Organisation-wide only: who owns it (elsewhere the workspace's user does). */
    owner?: string;
    pipeline?: string;
    stage?: string;
    stageProbability?: string;
    lost?: boolean;
    negotiatedPrice?: string;
    customFields?: Record<string, CustomValue>;
  },
): OpportunityCreateRequest {
  const body: { -readonly [K in keyof OpportunityCreateRequest]: OpportunityCreateRequest[K] } = {
    value: amount(draft.value),
    opportunity_date: draft.opportunity_date,
  };
  for (const field of OPTIONAL_TEXT) {
    if (draft[field].trim()) body[field] = MULTILINE.has(field) ? draft[field] : draft[field].trim();
  }
  if (target.owner) body.owner = target.owner;
  if (target.pipeline) body.pipeline = target.pipeline;
  if (target.stage) body.stage = target.stage;
  const probability = percent(draft.probability);
  if (probability !== null && !sameDecimal(probability, target.stageProbability)) body.probability = probability;
  if (draft.expected_close_date) body.expected_close_date = draft.expected_close_date;
  if (draft.description.trim()) body.description = draft.description;
  if (target.lost && draft.lost_reason.trim()) body.lost_reason = draft.lost_reason.trim();
  if (target.negotiatedPrice) body.negotiated_price = target.negotiatedPrice;
  const custom = Object.fromEntries(Object.entries(target.customFields ?? {}).filter(([, v]) => !isEmptyValue(v)));
  if (Object.keys(custom).length) body.custom_fields = custom;
  return body;
}

/** Only what changed, plus the version the edit started from. */
export function updateRequest(
  base: Draft,
  draft: Draft,
  version: number,
  custom?: { before: Record<string, CustomValue>; after: Record<string, CustomValue> },
): OpportunityUpdateRequest {
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
      case "account_name":
      case "customer_name":
        body[field] = draft[field].trim();
        break;
      default:
        body[field] = draft[field];
    }
  }
  if (custom) {
    const changes = changedCustomValues(custom.before, custom.after);
    if (Object.keys(changes).length) body.custom_fields = changes;
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

// --- custom fields ----------------------------------------------------------------------------
/** A custom value as the form holds it: text (also numbers, money and dates), a yes/no, or
 * the chosen option ids of a multiple choice. */
export type CustomValue = string | boolean | string[] | null;

export function isEmptyValue(value: CustomValue | undefined): boolean {
  return value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0);
}

/** The stored values of the given fields, in form shape. */
export function customDraft(fields: readonly CustomField[], stored: Record<string, unknown> | undefined): Record<string, CustomValue> {
  const draft: Record<string, CustomValue> = {};
  for (const field of fields) {
    const value = stored?.[field.id];
    if (field.type === "boolean") draft[field.id] = value === true ? true : value === false ? false : null;
    else if (field.type === "multi_select") draft[field.id] = Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
    else draft[field.id] = typeof value === "string" ? value : "";
  }
  return draft;
}

function sameValue(a: CustomValue | undefined, b: CustomValue | undefined): boolean {
  if (isEmptyValue(a) && isEmptyValue(b)) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v) => b.includes(v));
  return a === b;
}

/** What changed, with null for a value cleared (the server merges). */
export function changedCustomValues(before: Record<string, CustomValue>, after: Record<string, CustomValue>): Record<string, CustomValue> {
  const changes: Record<string, CustomValue> = {};
  for (const [id, value] of Object.entries(after)) {
    if (!sameValue(before[id], value)) changes[id] = isEmptyValue(value) ? null : value;
  }
  return changes;
}

/** Client checks for custom values (required ones, number and money formats). */
export function validateCustom(fields: readonly CustomField[], values: Record<string, CustomValue>, creating: boolean): Problems {
  const problems: Problems = {};
  for (const field of fields) {
    const value = values[field.id];
    const key = `custom_fields.${field.id}` as const;
    if (isEmptyValue(value)) {
      if (field.required && (creating || field.type !== "boolean")) problems[key] = [`Enter ${field.name}.`];
      continue;
    }
    if (field.type === "number" && typeof value === "string" && !/^-?\d{1,15}(\.\d{1,4})?$/.test(value.trim())) {
      problems[key] = ["Enter a number such as 42 or -3.5."];
    }
    if (field.type === "currency" && typeof value === "string") {
      const parsed = parseAmountInput(value);
      if (!parsed.ok) problems[key] = [parsed.error];
    }
  }
  return problems;
}

/** Values as the API takes them (money canonicalised, text trimmed). */
export function customRequest(fields: readonly CustomField[], values: Record<string, CustomValue>): Record<string, CustomValue> {
  const out: Record<string, CustomValue> = {};
  for (const field of fields) {
    const value = values[field.id];
    if (value === undefined) continue;
    if (field.type === "currency" && typeof value === "string" && value.trim()) {
      const parsed = parseAmountInput(value);
      out[field.id] = parsed.ok ? parsed.value : value;
    } else if (typeof value === "string" && field.type !== "long_text") {
      out[field.id] = value.trim();
    } else {
      out[field.id] = value;
    }
  }
  return out;
}
