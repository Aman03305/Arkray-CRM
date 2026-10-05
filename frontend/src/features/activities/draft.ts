/**
 * The task and meeting form's editable state and its conversions to API requests. Pure
 * functions, unit-tested without rendering: what is sent, what counts as a change, how an
 * edit conflict is merged. Times are typed as wall-clock times in the business time zone
 * (Asia/Kolkata, like every time on screen) and sent as UTC instants.
 */
import type { Activity, ActivityCreateRequest, ActivityPriority, ActivityUpdateRequest } from "@/lib/api/types";
import { fromBusinessDateTimeInput, toBusinessDateTimeInput } from "@/lib/format";

export type FormKind = "task" | "meeting";

/** Every value as the form holds it (text). */
export interface Draft {
  title: string;
  description: string;
  priority: ActivityPriority;
  /** "YYYY-MM-DD" business date, "" for no due date. */
  dueDate: string;
  /** "HH:mm"; the end of the working day unless changed. */
  dueTime: string;
  /** "YYYY-MM-DDTHH:mm" business wall-clock times. */
  startsAt: string;
  endsAt: string;
  location: string;
  meetingUrl: string;
}

export const DEFAULT_DUE_TIME = "18:00";
export const DEFAULT_MEETING_MINUTES = 30;

export const EMPTY_DRAFT: Draft = {
  title: "",
  description: "",
  priority: "normal",
  dueDate: "",
  dueTime: DEFAULT_DUE_TIME,
  startsAt: "",
  endsAt: "",
  location: "",
  meetingUrl: "",
};

/** API field -> its label (for "review these fields" after a conflict). */
export const FIELD_LABELS: Record<string, string> = {
  title: "Subject",
  description: "Description",
  priority: "Priority",
  due_at: "Due",
  starts_at: "Start",
  ends_at: "End",
  location: "Location",
  meeting_url: "Meeting link",
};

const FIELDS: Record<FormKind, readonly string[]> = {
  task: ["title", "description", "priority", "due_at"],
  meeting: ["title", "description", "starts_at", "ends_at", "location", "meeting_url"],
};

export function draftFromActivity(activity: Activity): Draft {
  const due = toBusinessDateTimeInput(activity.due_at);
  return {
    title: activity.title,
    description: activity.description,
    priority: activity.priority ?? "normal",
    dueDate: due ? due.slice(0, 10) : "",
    dueTime: due ? due.slice(11, 16) : DEFAULT_DUE_TIME,
    startsAt: toBusinessDateTimeInput(activity.starts_at),
    endsAt: toBusinessDateTimeInput(activity.ends_at),
    location: activity.location,
    meetingUrl: activity.meeting_url,
  };
}

/** The meeting end a new start suggests (30 minutes later), as a wall-clock input value. */
export function suggestedEnd(startsAt: string): string {
  const start = fromBusinessDateTimeInput(startsAt);
  if (!start) return "";
  return toBusinessDateTimeInput(new Date(new Date(start).getTime() + DEFAULT_MEETING_MINUTES * 60_000).toISOString());
}

/**
 * The end after the start moves from `previousStart` to `nextStart`: a meeting that had a
 * valid length keeps it (moving 11:00-12:00 to 15:00 gives 15:00-16:00, as calendars do);
 * otherwise an empty end gets the suggested one and a typed end stays as typed.
 */
export function endAfterStartChange(previousStart: string, previousEnd: string, nextStart: string): string {
  const before = fromBusinessDateTimeInput(previousStart);
  const end = fromBusinessDateTimeInput(previousEnd);
  const after = fromBusinessDateTimeInput(nextStart);
  if (before && end && after) {
    const length = new Date(end).getTime() - new Date(before).getTime();
    if (length > 0) return toBusinessDateTimeInput(new Date(new Date(after).getTime() + length).toISOString());
  }
  return previousEnd || suggestedEnd(nextStart);
}

function dueAt(draft: Draft): string | null {
  if (!draft.dueDate) return null;
  return fromBusinessDateTimeInput(`${draft.dueDate}T${draft.dueTime || DEFAULT_DUE_TIME}`);
}

/** The API value of each field, as compared and sent. */
function apiValues(kind: FormKind, draft: Draft): Record<string, string | null> {
  const values: Record<string, string | null> = {
    title: draft.title.trim().replace(/\s+/g, " "),
    description: draft.description.trim(),
  };
  if (kind === "task") {
    values.priority = draft.priority;
    values.due_at = dueAt(draft);
  } else {
    values.starts_at = fromBusinessDateTimeInput(draft.startsAt);
    values.ends_at = fromBusinessDateTimeInput(draft.endsAt);
    values.location = draft.location.trim().replace(/\s+/g, " ");
    values.meeting_url = draft.meetingUrl.trim();
  }
  return values;
}

export function changedFields(kind: FormKind, before: Draft, after: Draft): string[] {
  const a = apiValues(kind, before);
  const b = apiValues(kind, after);
  return FIELDS[kind].filter((field) => a[field] !== b[field]);
}

export type Problems = Partial<Record<"opportunity" | "title" | "due_at" | "starts_at" | "ends_at" | "meeting_url", string[]>>;

const DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Client-side checks (the server re-checks everything); each field's first problem. */
export function validateDraft(kind: FormKind, draft: Draft, { requireOpportunity = false, opportunity = "" } = {}): Problems {
  const problems: Problems = {};
  if (requireOpportunity && !opportunity) problems.opportunity = ["Choose the opportunity this is about."];
  if (!draft.title.trim()) problems.title = ["Enter a subject."];
  if (kind === "task" && draft.dueDate) {
    const due = dueAt(draft);
    if (!DATE.test(draft.dueDate) || !due || draft.dueDate < "2000-01-01" || draft.dueDate > "2099-12-31") {
      problems.due_at = ["Enter a date between 2000 and 2099."];
    }
  }
  if (kind === "meeting") {
    const start = fromBusinessDateTimeInput(draft.startsAt);
    const end = fromBusinessDateTimeInput(draft.endsAt);
    if (!start) problems.starts_at = ["Enter the start date and time."];
    if (!end) problems.ends_at = ["Enter the end date and time."];
    if (start && end) {
      const minutes = (new Date(end).getTime() - new Date(start).getTime()) / 60_000;
      if (minutes <= 0) problems.ends_at = ["The end must be after the start."];
      else if (minutes > 24 * 60) problems.ends_at = ["A meeting can last at most 24 hours."];
    }
    const url = draft.meetingUrl.trim();
    if (url && !/^https:\/\/[^\s/$.?#].[^\s]*$/i.test(url)) problems.meeting_url = ["Enter a link starting with https://."];
  }
  return problems;
}

/** `opportunity`: what it is about (its customer is implied, ADR-0027). */
export function createRequest(kind: FormKind, draft: Draft, opportunity: string): ActivityCreateRequest {
  const values = apiValues(kind, draft);
  const body: { -readonly [K in keyof ActivityCreateRequest]: ActivityCreateRequest[K] } = { type: kind, title: values.title ?? "", opportunity };
  if (values.description) body.description = draft.description;
  if (kind === "task") {
    body.priority = draft.priority;
    if (values.due_at) body.due_at = values.due_at;
  } else {
    body.starts_at = values.starts_at ?? "";
    body.ends_at = values.ends_at ?? "";
    if (values.location) body.location = values.location;
    if (values.meeting_url) body.meeting_url = values.meeting_url;
  }
  return body;
}

/** Only what changed, plus the version the edit started from. */
export function updateRequest(kind: FormKind, base: Draft, draft: Draft, version: number): ActivityUpdateRequest {
  const values = apiValues(kind, draft);
  const body: Record<string, string | number | null> = { version };
  for (const field of changedFields(kind, base, draft)) {
    body[field] = field === "description" ? draft.description : values[field] ?? null;
  }
  return body as ActivityUpdateRequest;
}

const DRAFT_KEYS: Record<string, readonly (keyof Draft)[]> = {
  title: ["title"],
  description: ["description"],
  priority: ["priority"],
  due_at: ["dueDate", "dueTime"],
  starts_at: ["startsAt"],
  ends_at: ["endsAt"],
  location: ["location"],
  meeting_url: ["meetingUrl"],
};

/**
 * An edit conflict (409): someone saved the activity after this form loaded it. Re-apply
 * this user's own changes on top of the latest version; report fields both people changed
 * to different values, so the user reviews them before saving again.
 */
export function mergeConflict(kind: FormKind, base: Draft, mine: Draft, latest: Draft): { merged: Draft; overlapping: string[] } {
  const myChanges = changedFields(kind, base, mine);
  const theirChanges = new Set(changedFields(kind, base, latest));
  const merged: Draft = { ...latest };
  for (const field of myChanges) {
    for (const key of DRAFT_KEYS[field] ?? []) (merged as unknown as Record<string, string>)[key] = mine[key];
  }
  const mineValues = apiValues(kind, mine);
  const latestValues = apiValues(kind, latest);
  const overlapping = myChanges.filter((f) => theirChanges.has(f) && mineValues[f] !== latestValues[f]);
  return { merged, overlapping };
}
