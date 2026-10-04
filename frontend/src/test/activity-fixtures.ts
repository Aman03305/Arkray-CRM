import type { Activity, ActivityListItem, ActivitySummary, TimelineEntry } from "@/lib/api/types";

import { LEAD_ID, RAHUL_ID } from "./fixtures";
import { OPPORTUNITY_ID } from "./pipeline-fixtures";

export const TASK_ID = "a1c1e000-0000-4000-8000-0000000000a1";
export const MEETING_ID = "a1c1e000-0000-4000-8000-0000000000a2";
export const NOTE_ID = "a1c1e000-0000-4000-8000-0000000000a3";

const RAHUL = { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true };
const LEAD_REF = { id: LEAD_ID, display_name: "Asha Mehta", organization_name: "Apollo Diagnostics", restricted: false };

export function makeActivity(overrides: Partial<Activity> = {}): Activity {
  const base: Activity = {
    id: TASK_ID,
    type: "task",
    title: "Send the revised quotation",
    status: "open",
    priority: "normal",
    due_at: "2026-10-05T12:30:00Z",
    starts_at: null,
    ends_at: null,
    is_overdue: false,
    completable: true,
    lead: LEAD_REF,
    opportunity: null,
    owner: RAHUL,
    created_by: RAHUL,
    completed_at: null,
    cancelled_at: null,
    archived_at: null,
    version: 1,
    created_at: "2026-10-01T04:30:00Z",
    updated_at: "2026-10-01T04:30:00Z",
    description: "Include the service contract.",
    location: "",
    meeting_url: "",
    completed_by: null,
    cancelled_by: null,
  };
  return { ...base, ...overrides };
}

export function makeMeeting(overrides: Partial<Activity> = {}): Activity {
  return makeActivity({
    id: MEETING_ID,
    type: "meeting",
    title: "Product demo",
    status: "scheduled",
    priority: null,
    due_at: null,
    starts_at: "2026-10-06T05:30:00Z",
    ends_at: "2026-10-06T06:30:00Z",
    completable: false,
    location: "Andheri office",
    meeting_url: "https://meet.example/demo",
    description: "Walk through the analyser.",
    opportunity: { id: OPPORTUNITY_ID, title: "Hospital Analyzer Project", status: "open", restricted: false },
    ...overrides,
  });
}

export function makeNote(overrides: Partial<Activity> = {}): Activity {
  return makeActivity({
    id: NOTE_ID,
    type: "note",
    title: "",
    status: null,
    priority: null,
    due_at: null,
    completable: false,
    description: "Prefers morning calls.",
    ...overrides,
  });
}

export function asListItem(activity: Activity, overrides: Partial<ActivityListItem> = {}): ActivityListItem {
  const { description, location, meeting_url, completed_by, cancelled_by, ...shared } = activity;
  void location;
  void meeting_url;
  void completed_by;
  void cancelled_by;
  return {
    ...shared,
    preview: description.slice(0, 240),
    preview_truncated: description.length > 240,
    ...overrides,
  };
}

export function page<T>(results: T[], next: string | null = null, previous: string | null = null) {
  return { results, next, previous };
}

export const SUMMARY: ActivitySummary = {
  open_tasks: 4,
  tasks_due_today: 1,
  overdue_tasks: 2,
  meetings_today: 1,
  upcoming_meetings: 3,
};

export function makeEntry(overrides: Partial<TimelineEntry> = {}): TimelineEntry {
  const base: TimelineEntry = {
    id: "e1",
    kind: "lead.created",
    occurred_at: "2026-09-20T04:30:00Z",
    actor: RAHUL,
    details: { status: "new", status_name: "New", owner: RAHUL },
    activity: null,
    opportunity: null,
  };
  return { ...base, ...overrides };
}

export function noteEntry(text: string, overrides: Partial<TimelineEntry> = {}): TimelineEntry {
  return makeEntry({
    id: "e2",
    kind: "note.added",
    occurred_at: "2026-10-01T05:00:00Z",
    details: {},
    activity: {
      id: NOTE_ID,
      type: "note",
      title: "",
      preview: text.slice(0, 240),
      preview_truncated: text.length > 240,
      status: null,
      due_at: null,
      starts_at: null,
      ends_at: null,
    },
    ...overrides,
  });
}
