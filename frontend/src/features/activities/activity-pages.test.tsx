import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivitiesView, ActivityView, OpportunityView } from "@/features/workspace/views";
import type { Activity, SearchOpportunity, SearchResults } from "@/lib/api/types";
import {
  asListItem,
  makeActivity,
  makeEntry,
  makeMeeting,
  makeNote,
  MEETING_ID,
  NOTE_ID,
  noteEntry,
  page,
  SUMMARY,
  TASK_ID,
} from "@/test/activity-fixtures";
import { adminViewer, LEAD_ID, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { makeCard, makeOpportunity, OPPORTUNITY_ID, OTHER_OPPORTUNITY_ID, PIPELINE_ROUTES } from "@/test/pipeline-fixtures";
import { apiError, createTestQueryClient, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { forgetActivityListState, presetActivityList } from "./list-state";

const nav = vi.hoisted(() => ({ pathname: "/activities/x", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const ME = "/api/v1/workspaces/me";
const LIST = `${ME}/activities`;
const DEAL = `${ME}/opportunities/${OPPORTUNITY_ID}`;
const SUMMARY_ROUTE = { [`GET ${ME}/activity-summary`]: { status: 200, body: SUMMARY } };
const activityUrl = (id: string, action = "") => `${ME}/activities/${id}${action ? `/${action}` : ""}`;
const SALES = { ...salesViewer, id: RAHUL_ID };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

/** The deal's page (own workspace): the deal, its pipeline, its open work and its History tab. */
function dealRoutes(timeline: unknown = { status: 200, body: page([]) }, extra: Record<string, unknown> = {}) {
  return {
    ...PIPELINE_ROUTES,
    [`GET ${DEAL}`]: { status: 200, body: makeOpportunity() },
    [`GET ${DEAL}/history`]: { status: 200, body: page([]) },
    [`GET ${DEAL}/negotiated-prices`]: { status: 200, body: page([]) },
    [`GET ${LIST}`]: { status: 200, body: page([]) },
    [`GET ${DEAL}/timeline`]: timeline,
    ...extra,
  } as Parameters<typeof mockApi>[0];
}

async function openTab(user: ReturnType<typeof userEvent.setup>, name: "History" | "Notes") {
  await user.click(await screen.findByRole("tab", { name }));
}

beforeEach(() => {
  nav.pathname = `/activities/${TASK_ID}`;
  nav.push.mockReset();
  forgetActivityListState();
});

describe("an activity's page", () => {
  it("completes, then reopens, a task with the versions it shows", async () => {
    let current: Activity = makeActivity({ version: 3 });
    const api = mockApi({
      [`GET ${activityUrl(TASK_ID)}`]: () => ({ status: 200, body: current }),
      [`POST ${activityUrl(TASK_ID, "complete")}`]: () => {
        current = makeActivity({ status: "completed", version: 4, completable: false, completed_by: SALES_REF, completed_at: "2026-10-03T05:00:00Z" });
        return { status: 200, body: current };
      },
      [`POST ${activityUrl(TASK_ID, "reopen")}`]: () => {
        current = makeActivity({ version: 5 });
        return { status: 200, body: current };
      },
    });
    renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Complete" }));
    expect(await screen.findByText("Marked as completed.")).toBeInTheDocument();
    expect(api.callsTo("POST", activityUrl(TASK_ID, "complete"))[0]!.body).toEqual({ version: 3 });
    expect(screen.getByText("Completed by")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reopen" }));
    expect(await screen.findByText("Reopened.")).toBeInTheDocument();
    expect(api.callsTo("POST", activityUrl(TASK_ID, "reopen"))[0]!.body).toEqual({ version: 4 });
  });

  it("cancelling asks first and explains a conflict", async () => {
    mockApi({
      [`GET ${activityUrl(TASK_ID)}`]: { status: 200, body: makeActivity() },
      [`POST ${activityUrl(TASK_ID, "cancel")}`]: apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Cancel task" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel task" }));
    expect(await screen.findByText(/Someone else changed this a moment ago/)).toBeInTheDocument();
  });

  it("a meeting's link is an https link that opens safely in a new tab; it can't be completed before it starts", async () => {
    nav.pathname = `/activities/${MEETING_ID}`;
    // Starts tomorrow, whenever the suite runs (Complete appears once a meeting has started).
    const tomorrow = Date.now() + 24 * 3600_000;
    const meeting = makeMeeting({ starts_at: new Date(tomorrow).toISOString(), ends_at: new Date(tomorrow + 3600_000).toISOString() });
    mockApi({ [`GET ${activityUrl(MEETING_ID)}`]: { status: 200, body: meeting } });
    renderWithProviders(<ActivityView activityId={MEETING_ID} />, { viewer: SALES });
    const link = await screen.findByRole("link", { name: /https:\/\/meet\.example\/demo/ });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("button", { name: "Mark as completed" })).not.toBeInTheDocument();
    expect(screen.getByText(/can be marked as completed once it has started/)).toBeInTheDocument();
  });

  it("completing a meeting marks activities and timelines stale, and no [\"leads\", ...] query", async () => {
    nav.pathname = `/activities/${MEETING_ID}`;
    const startedFiveMinutesAgo = new Date(Date.now() - 5 * 60_000).toISOString();
    const endsLater = new Date(Date.now() + 25 * 60_000).toISOString();
    const times = { starts_at: startedFiveMinutesAgo, ends_at: endsLater };
    const api = mockApi({
      [`GET ${activityUrl(MEETING_ID)}`]: { status: 200, body: makeMeeting({ ...times, completable: true }) },
      [`POST ${activityUrl(MEETING_ID, "complete")}`]: {
        status: 200,
        body: makeMeeting({ ...times, status: "completed", completable: false, version: 2, completed_by: SALES_REF, completed_at: endsLater }),
      },
    });
    // Whatever a lead screen may once have cached is not this write's business any more (ADR-0027).
    const client = createTestQueryClient();
    const leadQueries = [["leads", "detail", "me", LEAD_ID], ["leads", "list", "me"]];
    for (const key of leadQueries) client.setQueryData(key, { id: LEAD_ID });
    const timeline = ["timeline", "opportunity", "me", OPPORTUNITY_ID];
    client.setQueryData(timeline, { pages: [], pageParams: [] });
    renderWithProviders(<ActivityView activityId={MEETING_ID} />, { viewer: SALES, client });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mark as completed" }));
    expect(await screen.findByText("Marked as completed.")).toBeInTheDocument();
    expect(api.callsTo("POST", activityUrl(MEETING_ID, "complete"))[0]!.body).toEqual({ version: 1 });
    expect(client.getQueryState(timeline)!.isInvalidated).toBe(true);
    for (const key of leadQueries) expect(client.getQueryState(key)!.isInvalidated).toBe(false);
    expect(client.getQueryCache().findAll({ queryKey: ["leads"] }).some((query) => query.state.isInvalidated)).toBe(false);
  });

  it("only a note's author may edit it; others may still archive it", async () => {
    nav.pathname = `/activities/${NOTE_ID}`;
    const byAdmin = makeNote({ created_by: { id: "a1", full_name: "Anita Admin", is_active: true } });
    mockApi({ [`GET ${activityUrl(NOTE_ID)}`]: { status: 200, body: byAdmin } });
    renderWithProviders(<ActivityView activityId={NOTE_ID} />, { viewer: SALES });
    expect(await screen.findByText("Prefers morning calls.")).toBeInTheDocument();
    expect(screen.getByText("Anita Admin", { selector: "strong *, strong" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit note" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Archive" })).toBeInTheDocument();
  });

  it("the author's note edit survives a conflict: the text stays on screen", async () => {
    nav.pathname = `/activities/${NOTE_ID}`;
    let first = true;
    const api = mockApi({
      [`GET ${activityUrl(NOTE_ID)}`]: { status: 200, body: makeNote({ version: first ? 1 : 2 }) },
      [`PATCH ${activityUrl(NOTE_ID)}`]: (call: RecordedCall) => {
        if (first) {
          first = false;
          return apiError(409, "conflict", "Changed.");
        }
        return { status: 200, body: makeNote({ description: (call.body as { description: string }).description, version: 3 }) };
      },
    });
    renderWithProviders(<ActivityView activityId={NOTE_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit note" }));
    const box = screen.getByRole("textbox", { name: "Note text" });
    await user.clear(box);
    await user.type(box, "My careful rewrite");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByText(/changed this note while you were editing/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Note text" })).toHaveValue("My careful rewrite");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByText("Note saved.")).toBeInTheDocument();
    expect(api.callsTo("PATCH", activityUrl(NOTE_ID)).at(-1)!.body).toMatchObject({ description: "My careful rewrite" });
  });

  it("someone else's activity and a missing one look exactly alike", async () => {
    mockApi({ [`GET ${activityUrl(TASK_ID)}`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });
});

const SALES_REF = { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true };

describe("the customer on an activity (ADR-0027: no customer pages to link to)", () => {
  it("an activity's page names its customer as plain text, or says it is in another workspace", async () => {
    mockApi({ [`GET ${activityUrl(TASK_ID)}`]: { status: 200, body: makeActivity() } });
    const first = renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    const customer = await screen.findByText("Asha Mehta");
    expect(customer.closest("a")).toBeNull();
    expect(screen.getByText("Customer")).toBeInTheDocument();
    expect(screen.queryByText(/Lead/)).not.toBeInTheDocument();
    expect(document.querySelectorAll('a[href*="/leads"]')).toHaveLength(0);
    first.unmount();

    mockApi({ [`GET ${activityUrl(TASK_ID)}`]: { status: 200, body: makeActivity({ lead: { id: null, restricted: true } }) } });
    renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    expect(await screen.findByText("Customer in another workspace")).toBeInTheDocument();
    expect(screen.queryByText("Asha Mehta")).not.toBeInTheDocument();
  });

  it("the Activities list names each row's customer as plain text, or says it is in another workspace", async () => {
    nav.pathname = "/activities";
    mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: {
        status: 200,
        body: page([
          asListItem(makeActivity()),
          asListItem(makeMeeting({ lead: { id: null, restricted: true }, opportunity: { id: null, restricted: true } })),
        ]),
      },
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const table = await screen.findByRole("table", { name: "Activities" });
    expect(within(table).getByText("Asha Mehta").closest("a")).toBeNull();
    expect(within(table).getByText("Customer in another workspace")).toBeInTheDocument();
    expect(within(table).getByText("Opportunity in another workspace")).toBeInTheDocument();
    expect(document.querySelectorAll('a[href*="/leads"]')).toHaveLength(0);
  });
});

describe("the task form on a deal's page", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  });

  it("429: says how long to wait, keeps the typing, and Create works again with the same idempotency key", async () => {
    let attempt = 0;
    const api = mockApi(
      dealRoutes(undefined, {
        [`POST ${LIST}`]: () => {
          attempt += 1;
          return attempt === 1
            ? { ...apiError(429, "rate_limited", "Request was throttled. Expected available in 30 seconds."), headers: { "Retry-After": "30" } }
            : { status: 201, body: makeActivity({ title: "Call back" }) };
        },
      }),
    );
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    const openWork = await screen.findByRole("region", { name: "Open work" });
    await user.click(within(openWork).getByRole("button", { name: "New task" }));
    const dialog = screen.getByRole("dialog", { name: "New task" });
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Call back");
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Expected available in 30 seconds.");
    expect(within(dialog).getByRole("textbox", { name: "Subject" })).toHaveValue("Call back");
    const create = within(dialog).getByRole("button", { name: "Create task" });
    expect(create).not.toHaveAttribute("aria-disabled");
    await user.click(create);
    expect(await within(openWork).findByText("Task created.")).toBeInTheDocument();
    const [first, second] = api.callsTo("POST", LIST);
    expect(second!.headers["Idempotency-Key"]).toBe(first!.headers["Idempotency-Key"]);
  });

  it("has no picker: it says what it is about, focuses the first problem, then creates the task for the deal with an idempotency key reused on retry", async () => {
    let attempt = 0;
    const api = mockApi(
      dealRoutes(undefined, {
        [`POST ${LIST}`]: () => {
          attempt += 1;
          return attempt === 1 ? apiError(503, "service_unavailable", "Try again.") : { status: 201, body: makeActivity({ title: "Call back" }) };
        },
      }),
    );
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    const openWork = await screen.findByRole("region", { name: "Open work" });
    await user.click(within(openWork).getByRole("button", { name: "New task" }));
    const dialog = screen.getByRole("dialog", { name: "New task" });
    // Fixed: the deal's own page.
    expect(within(dialog).getByText("About:")).toHaveTextContent("About: Hospital Analyzer Project");
    expect(within(dialog).queryByRole("combobox", { name: "Opportunity" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("searchbox", { name: "Find an opportunity" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    expect(within(dialog).getByRole("textbox", { name: "Subject" })).toHaveFocus();
    expect(within(dialog).getByText("Enter a subject.")).toBeInTheDocument();
    expect(within(dialog).queryByText("Choose the opportunity this is about.")).not.toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Call back");
    await user.type(within(dialog).getByLabelText(/Due date/), "2026-10-03");
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    expect(await within(openWork).findByText("Task created.")).toBeInTheDocument();
    const posts = api.callsTo("POST", LIST);
    expect(posts).toHaveLength(2);
    expect(posts[0]!.body).toEqual({
      type: "task",
      title: "Call back",
      opportunity: OPPORTUNITY_ID,
      priority: "normal",
      due_at: "2026-10-03T12:30:00.000Z",
    });
    expect(posts[0]!.headers["Idempotency-Key"]).toBe(posts[1]!.headers["Idempotency-Key"]);
  });

  it("a meeting from the deal's Open work is about that deal too", async () => {
    const api = mockApi(dealRoutes(undefined, { [`POST ${LIST}`]: { status: 201, body: makeMeeting() } }));
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    const openWork = await screen.findByRole("region", { name: "Open work" });
    await user.click(within(openWork).getByRole("button", { name: "New meeting" }));
    const dialog = screen.getByRole("dialog", { name: "Schedule a meeting" });
    expect(within(dialog).getByText("About:")).toHaveTextContent("About: Hospital Analyzer Project");
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Demo");
    fireEvent.change(within(dialog).getByLabelText("Start"), { target: { value: "2026-10-06T11:00" } });
    await user.click(within(dialog).getByRole("button", { name: "Schedule meeting" }));
    expect(await within(openWork).findByText("Meeting scheduled.")).toBeInTheDocument();
    expect(api.callsTo("POST", LIST)[0]!.body).toEqual({
      type: "meeting",
      title: "Demo",
      opportunity: OPPORTUNITY_ID,
      starts_at: "2026-10-06T05:30:00.000Z",
      ends_at: "2026-10-06T06:00:00.000Z",
    });
  });

  it("an edit conflict keeps the typing and offers to apply it to the latest version", async () => {
    nav.pathname = `/activities/${TASK_ID}`;
    let latest = makeActivity({ version: 1 });
    const api = mockApi({
      [`GET ${activityUrl(TASK_ID)}`]: () => ({ status: 200, body: latest }),
      [`PATCH ${activityUrl(TASK_ID)}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 1
          ? apiError(409, "conflict", "Changed.")
          : { status: 200, body: makeActivity({ title: "Mine", description: "Theirs", version: 3 }) },
    });
    renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog", { name: "Edit task" });
    // What it is about can't be changed here (no deal on it: its customer is named).
    expect(within(dialog).getByText("About:")).toHaveTextContent("About: Asha Mehta");
    expect(within(dialog).queryByRole("combobox", { name: "Opportunity" })).not.toBeInTheDocument();
    const subject = within(dialog).getByRole("textbox", { name: "Subject" });
    await user.clear(subject);
    await user.type(subject, "Mine");
    latest = makeActivity({ description: "Theirs", version: 2 });
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    const apply = await within(dialog).findByRole("button", { name: "Apply my changes to the latest version" });
    expect(within(dialog).getByRole("textbox", { name: "Subject" })).toHaveValue("Mine");
    await user.click(apply);
    expect(within(dialog).getByRole("textbox", { name: /Description/ })).toHaveValue("Theirs");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.callsTo("PATCH", activityUrl(TASK_ID)).at(-1)!.body).toEqual({ version: 2, title: "Mine" });
  });
});

// --- a task or meeting opened outside a deal: it says which opportunity it is about ----------
const FOUND_ID = "7c6b5a49-3827-4e1f-9d0c-b1a2c3d4e5f8";
const FOUND: SearchOpportunity = {
  id: FOUND_ID,
  title: "Zeta Labs renewal",
  status: "open",
  stage: { id: "5ea9e000-0000-4000-8000-000000000003", name: "Proposal" },
  account_name: "Zeta Labs",
  customer_name: "Meera Iyer",
  lead: { id: "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2e", display_name: "Meera Iyer", organization_name: "Zeta Labs", restricted: false },
  owner: SALES_REF,
};

/** The global search's answer: only its opportunities group matters to the picker. */
function searchFor(query: string, opportunities: SearchOpportunity[]): SearchResults {
  const none = { results: [], has_more: false };
  return { query, terms: [query], leads: none, opportunities: { results: opportunities, has_more: false }, tasks: none, meetings: none, notes: none };
}

const RECENT = [
  makeCard(),
  makeCard({
    id: OTHER_OPPORTUNITY_ID,
    title: "Reagent contract",
    lead: { id: LEAD_ID, display_name: "Kiran Rao", organization_name: "Kiran Labs", restricted: false },
  }),
];

describe("a task or meeting from the Activities page", () => {
  beforeEach(() => {
    nav.pathname = "/activities";
  });

  it("picks the opportunity: recent open ones first, any other by search; it is required and sent as `opportunity`", async () => {
    const api = mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: { status: 200, body: page([]) },
      [`GET ${ME}/opportunities`]: { status: 200, body: page(RECENT) },
      [`GET ${ME}/search`]: (call: RecordedCall) => ({ status: 200, body: searchFor(call.query.get("q") ?? "", [FOUND]) }),
      [`POST ${LIST}`]: { status: 201, body: makeActivity({ title: "Call back" }) },
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    await screen.findByText("No activities yet");
    await user.click(screen.getByRole("button", { name: "New task" }));
    const dialog = screen.getByRole("dialog", { name: "New task" });
    const deal = within(dialog).getByRole("combobox", { name: "Opportunity" });
    await within(deal).findByRole("option", { name: "Reagent contract · Kiran Rao" });
    expect(within(deal).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose an opportunity",
      "Hospital Analyzer Project · Asha Mehta",
      "Reagent contract · Kiran Rao",
    ]);
    expect(Object.fromEntries(api.callsTo("GET", `${ME}/opportunities`)[0]!.query)).toEqual({ status: "open", page_size: "20" });

    // Nothing chosen: refused before anything is sent, with the picker focused.
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Call back");
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    expect(within(dialog).getByText("Choose the opportunity this is about.")).toBeInTheDocument();
    expect(deal).toHaveFocus();
    expect(api.callsTo("POST", LIST)).toHaveLength(0);

    // Two letters can't be searched: still the recent ones, and no search request.
    const find = within(dialog).getByRole("searchbox", { name: "Find an opportunity" });
    await user.type(find, "Ze");
    await new Promise((resolve) => setTimeout(resolve, 400)); // past the 300 ms debounce
    expect(api.callsTo("GET", `${ME}/search`)).toHaveLength(0);
    expect(within(deal).getByRole("option", { name: "Reagent contract · Kiran Rao" })).toBeInTheDocument();

    // A searchable query: the global search's opportunities ("Title · Customer"), once typing pauses.
    await user.type(find, "ta");
    await within(deal).findByRole("option", { name: "Zeta Labs renewal · Meera Iyer" });
    expect(api.callsTo("GET", `${ME}/search`).map((call) => call.query.get("q"))).toEqual(["Zeta"]);
    expect(within(deal).queryByRole("option", { name: "Reagent contract · Kiran Rao" })).not.toBeInTheDocument();

    await user.selectOptions(deal, FOUND_ID);
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(api.callsTo("POST", LIST)).toHaveLength(1));
    expect(api.callsTo("POST", LIST)[0]!.body).toEqual({ type: "task", title: "Call back", opportunity: FOUND_ID, priority: "normal" });
    expect(await screen.findByText('Task "Call back" created.')).toBeInTheDocument();
  });

  it("a meeting is about an opportunity too, chosen from the recent ones", async () => {
    const api = mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: { status: 200, body: page([]) },
      [`GET ${ME}/opportunities`]: { status: 200, body: page(RECENT) },
      [`POST ${LIST}`]: { status: 201, body: makeMeeting({ title: "Demo" }) },
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    await screen.findByText("No activities yet");
    await user.click(screen.getByRole("button", { name: "New meeting" }));
    const dialog = screen.getByRole("dialog", { name: "Schedule a meeting" });
    const deal = within(dialog).getByRole("combobox", { name: "Opportunity" });
    await within(deal).findByRole("option", { name: "Hospital Analyzer Project · Asha Mehta" });
    await user.selectOptions(deal, OPPORTUNITY_ID);
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Demo");
    fireEvent.change(within(dialog).getByLabelText("Start"), { target: { value: "2026-10-06T11:00" } });
    await user.click(within(dialog).getByRole("button", { name: "Schedule meeting" }));
    await waitFor(() => expect(api.callsTo("POST", LIST)).toHaveLength(1));
    const body = api.callsTo("POST", LIST)[0]!.body;
    expect(body).toMatchObject({ type: "meeting", title: "Demo", opportunity: OPPORTUNITY_ID });
    expect(body).not.toHaveProperty("lead");
  });
});

describe("the Activities list's opportunity filter", () => {
  beforeEach(() => {
    nav.pathname = "/activities";
  });

  it("More filters: the opportunity picker sets `opportunity` on the list request and counts as a filter; there is no lead filter", async () => {
    const api = mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: { status: 200, body: page([]) },
      [`GET ${ME}/opportunities`]: { status: 200, body: page(RECENT) },
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    await screen.findByText("No activities yet");
    await user.click(screen.getByRole("button", { name: "More filters" }));
    const deal = screen.getByRole("combobox", { name: "Opportunity" });
    expect(deal).toHaveAttribute("name", "filter-opportunity");
    await within(deal).findByRole("option", { name: "Hospital Analyzer Project · Asha Mehta" });
    expect(within(deal).getAllByRole("option")[0]).toHaveTextContent("Any opportunity");
    expect(screen.queryByRole("combobox", { name: /lead/i })).not.toBeInTheDocument();

    await user.selectOptions(deal, OPPORTUNITY_ID);
    await waitFor(() => expect(api.callsTo("GET", LIST).at(-1)!.query.get("opportunity")).toBe(OPPORTUNITY_ID));
    expect(screen.getByRole("button", { name: "More filters (1)" })).toBeInTheDocument();
    expect(await screen.findByText("Nothing matches these filters")).toBeInTheDocument();
    expect(api.calls.some((call) => call.query.has("lead"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(api.callsTo("GET", LIST).at(-1)!.query.has("opportunity")).toBe(false));
    expect(screen.getByRole("button", { name: "More filters" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Opportunity" })).toHaveValue("");
  });

  it("a deal's More in Activities opens the list filtered to it, named even when it isn't a recent one", async () => {
    presetActivityList("me", { status: "current", ordering: "scheduled", opportunity: FOUND_ID, opportunityLabel: "Zeta Labs renewal" });
    const api = mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: { status: 200, body: page([]) },
      [`GET ${ME}/opportunities`]: { status: 200, body: page(RECENT) },
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const deal = await screen.findByRole("combobox", { name: "Opportunity" });
    await within(deal).findByRole("option", { name: "Reagent contract · Kiran Rao" });
    expect(deal).toHaveValue(FOUND_ID);
    expect(within(deal).getByRole("option", { selected: true })).toHaveTextContent("Zeta Labs renewal");
    expect(api.callsTo("GET", LIST)[0]!.query.get("opportunity")).toBe(FOUND_ID);
    expect(screen.getByRole("button", { name: "More filters (1)" })).toBeInTheDocument();
  });
});

describe("a deal's timeline (its History tab)", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  });

  it("tells the deal's story in words, newest first, with restricted records unnamed", async () => {
    mockApi(
      dealRoutes({
        status: 200,
        body: page([
          noteEntry("y".repeat(300), { id: "e9" }),
          makeEntry({
            id: "e8",
            kind: "task.created",
            activity: { id: TASK_ID, type: "task", title: "Follow up", preview: "", preview_truncated: false, status: "open", due_at: null, starts_at: null, ends_at: null },
            opportunity: { id: null, restricted: true },
          }),
          makeEntry({
            id: "e7",
            kind: "opportunity.stage_changed",
            actor: { id: PRIYA_ID, full_name: "Priya Patel", is_active: false },
            details: { from_stage: "New", to_stage: "Proposal" },
          }),
          makeEntry({
            id: "e6",
            kind: "meeting.scheduled",
            details: { starts_at: "2026-10-06T05:30:00Z" },
            activity: { id: MEETING_ID, type: "meeting", title: "Product demo", preview: "", preview_truncated: false, status: "scheduled", due_at: null, starts_at: "2026-10-06T05:30:00Z", ends_at: "2026-10-06T06:30:00Z" },
          }),
          makeEntry({ id: "e5" }),
        ]),
      }),
    );
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await openTab(user, "History");
    const timeline = await screen.findByRole("region", { name: "Timeline" });
    const items = await within(timeline).findAllByRole("listitem");
    expect(items).toHaveLength(5);
    expect(items[0]).toHaveTextContent(`${"y".repeat(240)}…`);
    expect(within(items[0]!).getByRole("link", { name: "Read the whole note" })).toHaveAttribute("href", `/activities/${NOTE_ID}`);
    expect(items[1]).toHaveTextContent("Task created: Follow up");
    expect(items[1]).toHaveTextContent("an opportunity in another workspace");
    expect(items[2]).toHaveTextContent("Hospital Analyzer Project moved from New to Proposal");
    expect(items[2]).toHaveTextContent(/Priya Patel\s*\(deactivated\)/);
    expect(items[3]).toHaveTextContent("Meeting scheduled: Product demo for");
    expect(within(items[3]!).getByRole("link", { name: "Product demo" })).toHaveAttribute("href", `/activities/${MEETING_ID}`);
    expect(items[4]).toHaveTextContent("Opportunity Hospital Analyzer Project created in New");
    expect(within(items[4]!).getByRole("link", { name: "Hospital Analyzer Project" })).toHaveAttribute("href", `/pipeline/${OPPORTUNITY_ID}`);
    expect(within(timeline).getAllByRole("listitem").every((li) => li.querySelector("time"))).toBe(true);
  });

  it("shows older entries on request with the server's cursor", async () => {
    const api = mockApi(
      dealRoutes((call: RecordedCall) =>
        call.query.get("cursor")
          ? { status: 200, body: page([makeEntry({ id: "e1" })]) }
          : { status: 200, body: page([noteEntry("Newest", { id: "e5" })], `http://testserver${DEAL}/timeline?cursor=abc`) },
      ),
    );
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await openTab(user, "History");
    const timeline = await screen.findByRole("region", { name: "Timeline" });
    await user.click(await within(timeline).findByRole("button", { name: "Show older" }));
    expect(await within(timeline).findByText(/created in/)).toBeInTheDocument();
    expect(within(timeline).getByText("Newest")).toBeInTheDocument();
    expect(api.callsTo("GET", `${DEAL}/timeline`).at(-1)!.query.get("cursor")).toBe("abc");
  });

  it("a refused refetch hides entries instead of leaving them on screen", async () => {
    let refuse = false;
    mockApi(dealRoutes(() => (refuse ? apiError(404, "not_found", "Not found.") : { status: 200, body: page([noteEntry("Secret history")]) })));
    const { client } = renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: SALES });
    await openTab(userEvent.setup(), "History");
    expect(await screen.findByText("Secret history")).toBeInTheDocument();
    refuse = true;
    await act(async () => {
      await client.refetchQueries({ queryKey: ["timeline"] });
    });
    await waitFor(() => expect(screen.queryByText("Secret history")).not.toBeInTheDocument());
  });

  it("Rahul's deal history and open work never flash on Priya's deal page", async () => {
    const RAHUL = `/api/v1/workspaces/${RAHUL_ID}`;
    const PRIYA = `/api/v1/workspaces/${PRIYA_ID}`;
    const PRIYA_DEAL = OTHER_OPPORTUNITY_ID;
    const priyaTimeline = deferred<{ status: number; body: unknown }>();
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline/${OPPORTUNITY_ID}`;
    mockApi({
      ...PIPELINE_ROUTES,
      [`GET ${RAHUL}/opportunities/${OPPORTUNITY_ID}`]: { status: 200, body: makeOpportunity() },
      [`GET ${RAHUL}/activities`]: { status: 200, body: page([asListItem(makeActivity({ title: "Rahul's task" }))]) },
      [`GET ${RAHUL}/opportunities/${OPPORTUNITY_ID}/timeline`]: { status: 200, body: page([noteEntry("Rahul's private note")]) },
      [`GET ${PRIYA}/opportunities/${PRIYA_DEAL}`]: { status: 200, body: makeOpportunity({ id: PRIYA_DEAL, title: "Priya's deal" }) },
      [`GET ${PRIYA}/activities`]: { status: 200, body: page([]) },
      [`GET ${PRIYA}/opportunities/${PRIYA_DEAL}/timeline`]: () => priyaTimeline.promise,
    });
    const view = renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    expect(await screen.findByText("Rahul's task")).toBeInTheDocument();
    await openTab(user, "History");
    expect(await screen.findByText("Rahul's private note")).toBeInTheDocument();
    nav.pathname = `/admin/users/${PRIYA_ID}/pipeline/${PRIYA_DEAL}`;
    view.rerender(<OpportunityView opportunityId={PRIYA_DEAL} />);
    expect(screen.queryByText("Rahul's private note")).not.toBeInTheDocument();
    expect(screen.queryByText("Rahul's task")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 1, name: "Priya's deal" })).toBeInTheDocument();
    await openTab(user, "History");
    expect(await screen.findByText("Loading timeline")).toBeInTheDocument();
    expect(screen.queryByText("Rahul's private note")).not.toBeInTheDocument();
    await act(async () => priyaTimeline.resolve({ status: 200, body: page([noteEntry("Priya's note")]) }));
    expect(await screen.findByText("Priya's note")).toBeInTheDocument();
  });
});

describe("a note on a deal's page (its Notes tab)", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  });

  it("adds a note in two steps; a failure keeps the text, and the retry reuses its key", async () => {
    let attempt = 0;
    const api = mockApi(
      dealRoutes(undefined, {
        [`GET ${DEAL}/notes`]: { status: 200, body: page([]) },
        [`POST ${LIST}`]: () => {
          attempt += 1;
          return attempt === 1
            ? apiError(400, "validation_error", "Some fields are invalid.", { description: ["Remove the invisible or control characters from this text."] })
            : { status: 201, body: makeNote({ opportunity: { id: OPPORTUNITY_ID, title: "Hospital Analyzer Project", status: "open", restricted: false } }) };
        },
      }),
    );
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await openTab(user, "Notes");
    await user.click(await screen.findByRole("button", { name: "Add note" }));
    const box = screen.getByRole("textbox", { name: "Note" });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Write the note first.")).toBeInTheDocument();
    await user.type(box, "Prefers morning calls.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Remove the invisible or control characters from this text.")).toBeInTheDocument();
    expect(box).toHaveValue("Prefers morning calls.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Note saved.")).toBeInTheDocument();
    const posts = api.callsTo("POST", LIST);
    expect(posts).toHaveLength(2);
    expect(posts[1]!.body).toEqual({ type: "note", opportunity: OPPORTUNITY_ID, description: "Prefers morning calls." });
    expect(posts[0]!.headers["Idempotency-Key"]).toBe(posts[1]!.headers["Idempotency-Key"]);
  });
});
