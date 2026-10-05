/**
 * A fake backend holding two users' CRMs whose every record is marked with its owner
 * (RAHUL-ONLY-OPPORTUNITY, ₹1,11,111, ... / PRIYA-ONLY-OPPORTUNITY, ₹8,88,888, ...), for the admin user
 * workspace tests (docs/admin-user-workspace.md). Any request outside those two workspaces
 * (the organisation's "all", "me", a third id) is recorded as a leak and answered with a
 * LEAK marker, so a test can assert it was neither requested nor shown.
 *
 * Responses can be held (to finish in any order) or failed, per workspace and resource.
 */
import { vi } from "vitest";

import type { Activity, Dashboard, Opportunity } from "@/lib/api/types";

import { asListItem, makeActivity, makeMeeting, makeNote, page, SUMMARY } from "./activity-fixtures";
import { asRow, makeDashboard, makeDashboardLead } from "./dashboard-fixtures";
import { PRIYA_ID, RAHUL_ID } from "./fixtures";
import { makeBoard, makeCard, makeOpportunity, PIPELINES, STAGES } from "./pipeline-fixtures";
import { json, type RecordedCall } from "./render";

export interface Person {
  id: string;
  name: string;
  mark: "RAHUL-ONLY" | "PRIYA-ONLY";
  status: "active" | "invited" | "deactivated";
  amount: string;
  shown: string; // the amount as the UI shows it
  /** Their customer record (the API's lead): named on their deals and work, never opened. */
  leadId: string;
  opportunityId: string;
  taskId: string;
  meetingId: string;
  noteId: string;
}

export const RAHUL: Person = {
  id: RAHUL_ID,
  name: "Rahul Sharma",
  mark: "RAHUL-ONLY",
  status: "active",
  amount: "111111.00",
  shown: "₹1,11,111",
  leadId: "11111111-1111-4111-8111-000000000001",
  opportunityId: "11111111-1111-4111-8111-000000000002",
  taskId: "11111111-1111-4111-8111-000000000003",
  meetingId: "11111111-1111-4111-8111-000000000004",
  noteId: "11111111-1111-4111-8111-000000000005",
};

export const PRIYA: Person = {
  id: PRIYA_ID,
  name: "Priya Patel",
  mark: "PRIYA-ONLY",
  status: "active",
  amount: "888888.00",
  shown: "₹8,88,888",
  leadId: "88888888-8888-4888-8888-000000000001",
  opportunityId: "88888888-8888-4888-8888-000000000002",
  taskId: "88888888-8888-4888-8888-000000000003",
  meetingId: "88888888-8888-4888-8888-000000000004",
  noteId: "88888888-8888-4888-8888-000000000005",
};

/** Text that may only ever be on screen in `person`'s workspace. */
export function markersOf(person: Person): string[] {
  return [person.mark, person.shown, person.leadId, person.opportunityId, person.taskId, person.meetingId, person.noteId];
}

export const ACTOR = { id: "a1", full_name: "Anita Admin", is_active: true };
const ref = (p: Person) => ({ id: p.id, full_name: p.name, is_active: p.status === "active" });
const leadRef = (p: Person) => ({ id: p.leadId, display_name: `${p.mark}-LEAD`, organization_name: "", restricted: false });

export function opportunityOf(p: Person, overrides: Partial<Opportunity> = {}): Opportunity {
  return makeOpportunity({
    id: p.opportunityId,
    title: `${p.mark}-OPPORTUNITY`,
    lead: leadRef(p),
    owner: ref(p),
    created_by: ref(p),
    value: p.amount,
    weighted_value: p.amount,
    ...overrides,
  });
}

/** The person's opportunity as a board card or list row. */
export function cardOf(p: Person) {
  return makeCard({ id: p.opportunityId, title: `${p.mark}-OPPORTUNITY`, lead: leadRef(p), owner: ref(p), value: p.amount, weighted_value: p.amount, stage_id: STAGES.proposal.id });
}

export function activitiesOf(p: Person): Activity[] {
  const common = { owner: ref(p), created_by: ref(p), lead: leadRef(p), opportunity: null };
  return [
    makeActivity({ id: p.taskId, title: `${p.mark}-TASK`, ...common }),
    makeMeeting({ id: p.meetingId, title: `${p.mark}-MEETING`, ...common }),
    makeNote({ id: p.noteId, description: `${p.mark}-NOTE`, ...common }),
  ];
}

export function dashboardOf(p: Person): Dashboard {
  const [task, meeting] = activitiesOf(p);
  return makeDashboard({
    leads: { total: 1, new_today: 1 },
    pipeline: { pipeline_value: p.amount, weighted_pipeline: p.amount, open_count: 1 },
    new_leads: [makeDashboardLead({ id: p.leadId, display_name: `${p.mark}-LEAD`, owner: ref(p) })],
    upcoming_meetings: [asRow(meeting!)],
    next_tasks: [asRow(task!)],
  });
}

type Reply = { status: number; body?: unknown };
type Matcher = (call: RecordedCall & { workspace: string; rest: string }) => boolean;

export function workspaceWorld(people: Person[] = [RAHUL, PRIYA]) {
  const byId = new Map(people.map((p) => [p.id, p]));
  const calls: (RecordedCall & { workspace: string; rest: string })[] = [];
  const leaks: string[] = [];
  const holds: { match: Matcher; gate: Promise<void> }[] = [];
  const failures: { match: Matcher; reply: Reply }[] = [];

  function route(call: RecordedCall & { workspace: string; rest: string }, p: Person): Reply {
    const { method, rest } = call;
    const activities = activitiesOf(p);
    const ok = (body: unknown, status = 200): Reply => ({ status, body });
    const missing: Reply = { status: 404, body: { error: { code: "not_found", message: "Not found.", details: null, request_id: "req-404" } } };
    let m: RegExpExecArray | null;
    if (rest === "") return ok({ kind: "user", subject: { id: p.id, full_name: p.name, status: p.status } });
    if (rest === "/dashboard") return ok(dashboardOf(p));
    if (rest === "/pipelines") return ok(PIPELINES); // this workspace's pipelines
    if (rest === "/pipeline-board") {
      const board = makeBoard([cardOf(p)]);
      return ok({ ...board, totals: { pipeline_value: p.amount, weighted_pipeline: p.amount, open_count: 1 } });
    }
    // The workspace's opportunities (e.g. the recent open ones an activity can be about).
    if (rest === "/opportunities" && method === "GET") return ok(page([cardOf(p)]));
    if (rest === "/opportunities" && method === "POST") return ok(opportunityOf(p, { created_by: ACTOR }), 201);
    if ((m = /^\/opportunities\/([^/]+)(\/.*)?$/.exec(rest))) {
      if (m[1] !== p.opportunityId) return missing;
      if (!m[2]) return ok(opportunityOf(p, { version: method === "PATCH" ? 3 : 2 }));
      if (m[2] === "/history" || m[2] === "/timeline") return ok(page([]));
      if (m[2] === "/assign" && method === "POST") {
        const to = byId.get((call.body as { owner?: string } | undefined)?.owner ?? "");
        return to ? ok(opportunityOf(p, { owner: ref(to), version: 3 })) : missing;
      }
      return missing;
    }
    if (rest === "/activities" && method === "GET") return ok(page(activities.map((a) => asListItem(a))));
    if (rest === "/activities" && method === "POST") return ok({ ...activities[0]!, created_by: ACTOR }, 201);
    if (rest === "/activity-summary") return ok(SUMMARY);
    if ((m = /^\/activities\/([^/]+)(\/.*)?$/.exec(rest))) {
      const activity = activities.find((a) => a.id === m![1]);
      if (!activity) return missing;
      if (!m[2]) return ok(activity);
      if (m[2] === "/complete") return ok({ ...activity, status: "completed", completed_by: ACTOR, version: 2 });
      return missing;
    }
    return missing;
  }

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.pathname === "/api/v1/auth/csrf") {
      document.cookie = "arkray_csrftoken=test-csrf-token; path=/";
      return json(204);
    }
    if (url.pathname === "/api/v1/config/pipelines") return json(200, PIPELINES);
    // Who can own records: shared, not any workspace's data.
    if (url.pathname === "/api/v1/assignees") {
      return json(200, { results: people.map((p) => ({ id: p.id, full_name: p.name, email: `${p.name.toLowerCase().replace(/\s+/g, ".")}@example.test` })), next: null, previous: null });
    }
    const m =/^\/api\/v1\/workspaces\/([^/]+)(\/.*)?$/.exec(url.pathname);
    const call = {
      method,
      path: url.pathname,
      query: url.searchParams,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
      workspace: m?.[1] ?? "",
      rest: m?.[2] ?? "",
    };
    calls.push(call);
    for (const hold of holds) if (hold.match(call)) await hold.gate;
    if (init?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const failure = failures.find((f) => f.match(call));
    if (failure) return json(failure.reply.status, failure.reply.body);
    const person = m ? byId.get(m[1]!) : undefined;
    if (!person) {
      leaks.push(`${method} ${url.pathname}`);
      return json(200, { results: [{ id: "leak", display_name: "LEAK-ORGANISATION-DATA" }], next: null, previous: null });
    }
    const reply = route(call, person);
    return json(reply.status, reply.body);
  });
  vi.stubGlobal("fetch", fetchMock);

  return {
    calls,
    leaks,
    /** Hold matching responses until the returned function is called. */
    hold(match: Matcher): () => void {
      let release!: () => void;
      holds.push({ match, gate: new Promise<void>((resolve) => (release = resolve)) });
      return () => release();
    },
    fail(match: Matcher, reply: Reply) {
      failures.push({ match, reply });
    },
    /** Every API path this page called that isn't this workspace's or shared configuration. */
    foreignCalls(workspace: string) {
      return calls.filter((c) => c.workspace !== workspace).map((c) => `${c.method} ${c.path}`);
    },
  };
}

export const inWorkspace = (p: Person): Matcher => (call) => call.workspace === p.id;
