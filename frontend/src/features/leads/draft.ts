/**
 * The lead form's editable state and its conversions to API requests. Pure functions, so
 * the rules (what is sent, what counts as a change, how an edit conflict is merged) are
 * unit-tested without rendering anything.
 */
import type { Lead, LeadCreateRequest, LeadUpdateRequest, Rating } from "@/lib/api/types";
import { fromBusinessDateTimeInput, toBusinessDateTimeInput } from "@/lib/format";

export const PROFILE_FIELDS = [
  "first_name",
  "last_name",
  "organization_name",
  "job_title",
  "email",
  "phone",
  "mobile",
  "alternate_phone",
  "address_line_1",
  "address_line_2",
  "city",
  "state",
  "postal_code",
  "country",
  "source",
  "rating",
  "last_contacted_at",
  "description",
] as const;

export type ProfileField = (typeof PROFILE_FIELDS)[number];
/** Every value as the form holds it: text; "" means empty; last contact as a local input value. */
export type Draft = Record<ProfileField, string>;

export const FIELD_LABELS: Record<ProfileField, string> = {
  first_name: "First name",
  last_name: "Last name",
  organization_name: "Organization",
  job_title: "Job title",
  email: "Email",
  phone: "Phone",
  mobile: "Mobile",
  alternate_phone: "Alternate phone",
  address_line_1: "Address line 1",
  address_line_2: "Address line 2",
  city: "City",
  state: "State / region",
  postal_code: "Postal code",
  country: "Country",
  source: "Source",
  rating: "Rating",
  last_contacted_at: "Last contacted",
  description: "Description",
};

export const EMPTY_DRAFT: Draft = Object.fromEntries(PROFILE_FIELDS.map((f) => [f, ""])) as Draft;

export function draftFromLead(lead: Lead): Draft {
  return {
    first_name: lead.first_name,
    last_name: lead.last_name,
    organization_name: lead.organization_name,
    job_title: lead.job_title,
    email: lead.email,
    phone: lead.phone,
    mobile: lead.mobile,
    alternate_phone: lead.alternate_phone,
    address_line_1: lead.address_line_1,
    address_line_2: lead.address_line_2,
    city: lead.city,
    state: lead.state,
    postal_code: lead.postal_code,
    country: lead.country,
    source: lead.source?.key ?? "",
    rating: lead.rating ?? "",
    last_contacted_at: toBusinessDateTimeInput(lead.last_contacted_at),
    description: lead.description,
  };
}

type Values = { -readonly [K in keyof LeadUpdateRequest]: LeadUpdateRequest[K] };

function apiValue(field: ProfileField, value: string): Values[ProfileField] {
  const text = value.trim();
  switch (field) {
    case "source":
      return text || null;
    case "rating":
      return (text || null) as Rating | null;
    case "last_contacted_at":
      return text ? fromBusinessDateTimeInput(text) : null;
    default:
      return text;
  }
}

function comparable(field: ProfileField, value: string): string {
  return field === "description" ? value.trim() : value.trim().replace(/\s+/g, " ");
}

/** Fields whose value differs between two drafts (ignoring surrounding whitespace). */
export function changedFields(before: Draft, after: Draft): ProfileField[] {
  return PROFILE_FIELDS.filter((f) => comparable(f, before[f]) !== comparable(f, after[f]));
}

export function createRequest(draft: Draft, extra: { status?: string; owner?: string }): LeadCreateRequest {
  const body: Values & { status?: string; owner?: string } = {};
  for (const field of PROFILE_FIELDS) {
    const value = apiValue(field, draft[field]);
    if (value !== "" && value !== null) (body as Record<string, unknown>)[field] = value;
  }
  if (extra.status) body.status = extra.status;
  if (extra.owner) body.owner = extra.owner;
  return body;
}

/** Only what changed, plus the version the edit started from. */
export function updateRequest(base: Draft, draft: Draft, version: number): LeadUpdateRequest {
  const body: Values = { version };
  for (const field of changedFields(base, draft)) {
    (body as Record<string, unknown>)[field] = apiValue(field, draft[field]);
  }
  return body;
}

/**
 * An edit conflict (409): someone saved the lead after this form loaded it. Re-apply this
 * user's own changes on top of the latest version; report fields both people changed to
 * different values, so the user reviews them before saving again.
 */
export function mergeConflict(base: Draft, mine: Draft, latest: Draft): { merged: Draft; overlapping: ProfileField[] } {
  const myChanges = changedFields(base, mine);
  const theirChanges = new Set(changedFields(base, latest));
  const merged = { ...latest };
  for (const field of myChanges) merged[field] = mine[field];
  const overlapping = myChanges.filter((f) => theirChanges.has(f) && comparable(f, latest[f]) !== comparable(f, mine[f]));
  return { merged, overlapping };
}

export function hasIdentity(draft: Draft): boolean {
  return Boolean(draft.first_name.trim() || draft.last_name.trim() || draft.organization_name.trim());
}
