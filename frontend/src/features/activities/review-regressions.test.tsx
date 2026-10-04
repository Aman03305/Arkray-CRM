/**
 * Regression tests for the Phase 4 adversarial frontend review (docs/testing.md): one per
 * confirmed finding, each reproducing the reviewer's scenario and asserting the corrected
 * behaviour. Plus the live-walkthrough finding about reassignments (an administrator's
 * lead page refetching its timeline where the lead no longer is).
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { forgetActivityListState } from "@/features/activities/list-state";
import { ActivitiesView, ActivityView, LeadView } from "@/features/workspace/views";
import type { Activity } from "@/lib/api/types";
import { asListItem, makeActivity, makeEntry, makeMeeting, makeNote, MEETING_ID, NOTE_ID, page, SUMMARY, TASK_ID } from "@/test/activity-fixtures";
import { adminViewer, LEAD_ID, LEAD_OPTIONS, makeLead, makeLeadListItem, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { apiError, createTestQueryClient, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/activities", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const ME = "/api/v1/workspaces/me";
const LIST = `${ME}/activities`;
const SUMMARY_ROUTE = { [`GET ${ME}/activity-summary`]: { status: 200, body: SUMMARY } };
const LEAD_ROUTES = {
  "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS },
  [`GET ${ME}/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
};
const activityUrl = (id: string, action = "") => `${ME}/activities/${id}${action ? `/${action}` : ""}`;
const SALES = { ...salesViewer, id: RAHUL_ID };
const SALES_REF = { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true };

type Reply = { status: number; body: unknown };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

/** The app's QueryClient keeps data fresh for 30 s (lib/query-client.ts). */
function productionLikeClient() {
  const client = createTestQueryClient();
  client.setDefaultOptions({ queries: { retry: false, gcTime: Infinity, staleTime: 30_000 } });
  return client;
}

beforeEach(() => {
  nav.pathname = "/activities";
  nav.push.mockReset();
  forgetActivityListState();
});

describe("refused data leaves the screen (R1, P3)", () => {
  it("an activity page whose refetch is refused (403) stops showing the activity", async () => {
    nav.pathname = `/activities/${TASK_ID}`;
    let refuse = false;
    mockApi({
      [`GET ${activityUrl(TASK_ID)}`]: () =>
        refuse ? apiError(403, "permission_denied", "You do not have permission to perform this action.") : { status: 200, body: makeActivity({ description: "Offer them 12% off" }) },
    });
    const { client } = renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    expect(await screen.findByText("Offer them 12% off")).toBeInTheDocument();
    refuse = true;
    await act(async () => {
      await client.refetchQueries({ queryKey: ["activities"] });
    });
    expect(await screen.findByText("You can't view this activity")).toBeInTheDocument();
    expect(screen.queryByText("Offer them 12% off")).not.toBeInTheDocument();
  });

  it("the Activities page drops the summary counts the server now refuses", async () => {
    let refuse = false;
    mockApi({
      [`GET ${ME}/activity-summary`]: () => (refuse ? apiError(403, "permission_denied", "Denied.") : { status: 200, body: { ...SUMMARY, open_tasks: 41 } }),
      [`GET ${LIST}`]: () => (refuse ? apiError(403, "permission_denied", "Denied.") : { status: 200, body: page([asListItem(makeActivity())]) }),
    });
    const { client } = renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    expect(await screen.findByText("41")).toBeInTheDocument();
    refuse = true;
    await act(async () => {
      await client.refetchQueries({ queryKey: ["activities"] });
    });
    expect(await screen.findByText("You can't view these activities")).toBeInTheDocument();
    expect(screen.queryByText("41")).not.toBeInTheDocument();
  });
});

describe("cached list rows follow writes made elsewhere (P2-1)", () => {
  it("edit on the activity page, Back to the cached list, Complete: sends the new version", async () => {
    let current: Activity = makeActivity({ version: 1 });
    let slowList: ReturnType<typeof deferred<Reply>> | null = null;
    const api = mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: () => (slowList ? slowList.promise : { status: 200, body: page([asListItem(current)]) }),
      [`GET ${activityUrl(TASK_ID)}`]: () => ({ status: 200, body: current }),
      [`PATCH ${activityUrl(TASK_ID)}`]: (call: RecordedCall) => {
        current = makeActivity({ ...current, ...(call.body as Partial<Activity>), version: current.version + 1 });
        return { status: 200, body: current };
      },
      [`POST ${activityUrl(TASK_ID, "complete")}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version !== current.version
          ? apiError(409, "conflict", "The record was changed by someone else. Reload and try again.")
          : { status: 200, body: (current = makeActivity({ ...current, status: "completed", completable: false, version: current.version + 1 })) },
    });
    const view = renderWithProviders(<ActivitiesView />, { viewer: SALES, client: productionLikeClient() });
    const user = userEvent.setup();
    await screen.findAllByRole("button", { name: "Complete Send the revised quotation" });

    nav.pathname = `/activities/${TASK_ID}`;
    view.rerender(<ActivityView activityId={TASK_ID} />);
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog", { name: "Edit task" });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Priority" }), "high");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await screen.findByText("Changes saved.");

    // Back to the list; its reload is slow, so the cached rows show meanwhile.
    slowList = deferred();
    nav.pathname = "/activities";
    view.rerender(<ActivitiesView />);
    const [complete] = await screen.findAllByRole("button", { name: "Complete Send the revised quotation" });
    await user.click(complete!);
    await waitFor(() => expect(api.callsTo("POST", activityUrl(TASK_ID, "complete"))).toHaveLength(1));
    expect(api.callsTo("POST", activityUrl(TASK_ID, "complete"))[0]!.body).toEqual({ version: 2 });
    expect(screen.queryByText(/changed by someone else/)).not.toBeInTheDocument();
  });

  it("a row just completed stops offering Complete before the list reloads", async () => {
    let current: Activity = makeActivity({ version: 1 });
    let slowList: ReturnType<typeof deferred<Reply>> | null = null;
    mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: () => (slowList ? slowList.promise : { status: 200, body: page([asListItem(current)]) }),
      [`POST ${activityUrl(TASK_ID, "complete")}`]: (call: RecordedCall) => {
        if ((call.body as { version: number }).version !== current.version) return apiError(409, "conflict", "Changed.");
        slowList = deferred();
        return { status: 200, body: (current = makeActivity({ status: "completed", completable: false, version: 2 })) };
      },
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    const [first] = await screen.findAllByRole("button", { name: "Complete Send the revised quotation" });
    await user.click(first!);
    await screen.findByText("Send the revised quotation completed.");
    expect(screen.queryAllByRole("button", { name: "Complete Send the revised quotation" })).toHaveLength(0);
    expect(within(screen.getByRole("table", { name: "Activities" })).getByText("Completed")).toBeInTheDocument();
  });

  it("a row's menu doesn't open while an action on that row is running", async () => {
    const post = deferred<Reply>();
    mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: { status: 200, body: page([asListItem(makeActivity())]) },
      [`POST ${activityUrl(TASK_ID, "complete")}`]: () => post.promise,
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Activities" });
    await user.click(within(table).getByRole("button", { name: "Complete Send the revised quotation" }));
    const menu = within(table).getByRole("button", { name: "Actions for Send the revised quotation" });
    await waitFor(() => expect(menu).toHaveAttribute("aria-disabled", "true"));
    await user.click(menu);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    await act(async () => post.resolve({ status: 200, body: makeActivity({ status: "completed", completable: false, version: 2 }) }));
    await waitFor(() => expect(menu).not.toHaveAttribute("aria-disabled"));
  });
});

describe("focus stays on the page after in-page actions (P2-2)", () => {
  it("Complete on the activity page moves focus to the message", async () => {
    nav.pathname = `/activities/${TASK_ID}`;
    let current = makeActivity({ version: 1 });
    mockApi({
      [`GET ${activityUrl(TASK_ID)}`]: () => ({ status: 200, body: current }),
      [`POST ${activityUrl(TASK_ID, "complete")}`]: () => ({
        status: 200,
        body: (current = makeActivity({ status: "completed", completable: false, version: 2, completed_by: SALES_REF, completed_at: "2026-10-03T05:00:00Z" })),
      }),
    });
    renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    (await screen.findByRole("button", { name: "Complete" })).focus();
    await user.keyboard("{Enter}");
    const message = await screen.findByText("Marked as completed.");
    await waitFor(() => expect(document.activeElement).toContainElement(message));
  });

  it("Complete in a lead's Open work card moves focus to the card's message", async () => {
    nav.pathname = `/leads/${LEAD_ID}`;
    let done = false;
    mockApi({
      ...LEAD_ROUTES,
      [`GET ${ME}/leads/${LEAD_ID}/timeline`]: { status: 200, body: page([]) },
      [`GET ${LIST}`]: () => ({ status: 200, body: page(done ? [] : [asListItem(makeActivity())]) }),
      [`POST ${activityUrl(TASK_ID, "complete")}`]: () => {
        done = true;
        return { status: 200, body: makeActivity({ status: "completed", completable: false, version: 2 }) };
      },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    const openWork = await screen.findByRole("region", { name: "Open work" });
    (await within(openWork).findByRole("button", { name: "Complete Send the revised quotation" })).focus();
    await user.keyboard("{Enter}");
    await within(openWork).findByText("No open tasks or scheduled meetings.");
    expect(document.activeElement).toContainElement(within(openWork).getByText("Send the revised quotation completed."));
  });

  it("saving an edited note returns focus to Edit note", async () => {
    nav.pathname = `/activities/${NOTE_ID}`;
    mockApi({
      [`GET ${activityUrl(NOTE_ID)}`]: { status: 200, body: makeNote() },
      [`PATCH ${activityUrl(NOTE_ID)}`]: (call: RecordedCall) => ({
        status: 200,
        body: makeNote({ description: (call.body as { description: string }).description, version: 2 }),
      }),
    });
    renderWithProviders(<ActivityView activityId={NOTE_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit note" }));
    await user.type(screen.getByRole("textbox", { name: "Note text" }), " Not before 10.");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    await screen.findByText("Note saved.");
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit note" })).toHaveFocus());
  });

  it("Show older moves focus to the first entry it added (the button may be gone)", async () => {
    nav.pathname = `/leads/${LEAD_ID}`;
    mockApi({
      ...LEAD_ROUTES,
      [`GET ${LIST}`]: { status: 200, body: page([]) },
      [`GET ${ME}/leads/${LEAD_ID}/timeline`]: (call: RecordedCall) =>
        call.query.get("cursor")
          ? { status: 200, body: page([makeEntry({ id: "e1" })]) }
          : { status: 200, body: page([makeEntry({ id: "e2", kind: "lead.archived", details: {} })], `http://testserver${ME}/leads/${LEAD_ID}/timeline?cursor=abc`) },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    (await screen.findByRole("button", { name: "Show older" })).focus();
    await user.keyboard("{Enter}");
    const added = await screen.findByText(/Lead created/);
    await waitFor(() => expect(document.activeElement).toContainElement(added));
    expect(screen.queryByRole("button", { name: "Show older" })).not.toBeInTheDocument();
  });
});

describe("a note box is read-only while it saves (P2-3)", () => {
  it("the quick note box on a lead page: nothing typed meanwhile is lost", async () => {
    nav.pathname = `/leads/${LEAD_ID}`;
    const post = deferred<Reply>();
    mockApi({
      ...LEAD_ROUTES,
      [`GET ${ME}/leads/${LEAD_ID}/timeline`]: { status: 200, body: page([]) },
      [`GET ${LIST}`]: { status: 200, body: page([]) },
      [`POST ${LIST}`]: () => post.promise,
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Add a note" });
    await user.type(box, "Called Asha.");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(box).toHaveAttribute("readonly");
    await user.type(box, " She wants the AMC quote by Friday.");
    expect(box).toHaveValue("Called Asha.");
    await act(async () => post.resolve({ status: 201, body: makeNote({ description: "Called Asha." }) }));
    await screen.findByText("Note saved.");
    expect(box).not.toHaveAttribute("readonly");
  });

  it("editing a note in place", async () => {
    nav.pathname = `/activities/${NOTE_ID}`;
    const patch = deferred<Reply>();
    mockApi({
      [`GET ${activityUrl(NOTE_ID)}`]: { status: 200, body: makeNote() },
      [`PATCH ${activityUrl(NOTE_ID)}`]: () => patch.promise,
    });
    renderWithProviders(<ActivityView activityId={NOTE_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit note" }));
    const box = screen.getByRole("textbox", { name: "Note text" });
    await user.type(box, " Not before 10.");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(box).toHaveAttribute("readonly");
    await user.type(box, " Speak to the lab manager.");
    expect(box).toHaveValue("Prefers morning calls. Not before 10.");
    await act(async () => patch.resolve({ status: 200, body: makeNote({ description: "Prefers morning calls. Not before 10.", version: 2 }) }));
    await screen.findByText("Note saved.");
  });
});

describe("a refused create is explained (R5, P3)", () => {
  it("a retried create whose task has since left the workspace shows the server's message", async () => {
    const api = mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: { status: 200, body: page([]) },
      [`GET ${ME}/leads`]: { status: 200, body: page([makeLeadListItem()]) },
      [`GET ${ME}/opportunities`]: { status: 200, body: page([]) },
      [`POST ${LIST}`]: apiError(409, "conflict", "This was already created by an earlier request and has since left this workspace."),
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    await screen.findByText("No activities yet");
    await user.click(screen.getByRole("button", { name: "New task" }));
    const dialog = screen.getByRole("dialog", { name: "New task" });
    await within(dialog).findByRole("option", { name: /Asha Mehta/ });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Lead" }), LEAD_ID);
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Call back");
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(api.callsTo("POST", LIST)).toHaveLength(1));
    expect(await within(dialog).findByText(/already created by an earlier request/)).toBeInTheDocument();
  });
});

describe("a note changed while its author edits it (R6, P3)", () => {
  it("archived meanwhile: says so and doesn't invite another save", async () => {
    nav.pathname = `/activities/${NOTE_ID}`;
    let archived = false;
    mockApi({
      [`GET ${activityUrl(NOTE_ID)}`]: () => ({ status: 200, body: archived ? makeNote({ archived_at: "2026-10-03T06:00:00Z", version: 2 }) : makeNote() }),
      [`PATCH ${activityUrl(NOTE_ID)}`]: apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<ActivityView activityId={NOTE_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit note" }));
    await user.type(screen.getByRole("textbox", { name: "Note text" }), " Not before 10.");
    archived = true;
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByText("This note was archived while you were editing")).toBeInTheDocument();
    expect(screen.queryByText(/save again/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save note" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Note text" })).toHaveValue("Prefers morning calls. Not before 10.");
  });

  it("edited meanwhile: their version is shown under the author's text", async () => {
    nav.pathname = `/activities/${NOTE_ID}`;
    let latest = makeNote();
    mockApi({
      [`GET ${activityUrl(NOTE_ID)}`]: () => ({ status: 200, body: latest }),
      [`PATCH ${activityUrl(NOTE_ID)}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version !== latest.version
          ? apiError(409, "conflict", "Changed.")
          : { status: 200, body: makeNote({ description: (call.body as { description: string }).description, version: latest.version + 1 }) },
    });
    renderWithProviders(<ActivityView activityId={NOTE_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit note" }));
    await user.type(screen.getByRole("textbox", { name: "Note text" }), " Not before 10.");
    latest = makeNote({ description: "Prefers morning calls. Budget signed off by the CFO on 2 Oct.", version: 2 });
    await user.click(screen.getByRole("button", { name: "Save note" }));
    await screen.findByText(/changed this note while you were editing/);
    expect(screen.getByText(/Budget signed off by the CFO/)).toBeInTheDocument();
  });
});

describe("summary shortcuts open lists of exactly what they count (R7, P3)", () => {
  // A server-faithful list: cancelled=false leaves cancelled meetings out; upcoming=true
  // means scheduled from now on (not last week's meeting still awaiting an outcome).
  const cancelledToday = asListItem(makeMeeting({ id: "a1c1e000-0000-4000-8000-0000000000b1", title: "Cancelled demo", status: "cancelled" }));
  const heldToday = asListItem(makeMeeting({ id: "a1c1e000-0000-4000-8000-0000000000b2", title: "Held demo", status: "completed" }));
  const pastScheduled = asListItem(makeMeeting({ id: "a1c1e000-0000-4000-8000-0000000000b3", title: "Last week's visit", is_overdue: true, completable: true }));
  const nextWeek = asListItem(makeMeeting({ id: "a1c1e000-0000-4000-8000-0000000000b4", title: "Next week's demo" }));

  function listRoute(call: RecordedCall) {
    if (call.query.get("date_from")) {
      const today = [cancelledToday, heldToday];
      return { status: 200, body: page(call.query.get("cancelled") === "false" ? today.filter((a) => a.status !== "cancelled") : today) };
    }
    if (call.query.get("upcoming") === "true") return { status: 200, body: page([nextWeek]) };
    if (call.query.get("status") === "scheduled") return { status: 200, body: page([pastScheduled, nextWeek]) };
    return { status: 200, body: page([]) };
  }

  it.each([
    ["Meetings today", "Held demo", "Cancelled demo", { cancelled: "false", status: null, upcoming: null }],
    ["Upcoming meetings", "Next week's demo", "Last week's visit", { cancelled: null, status: null, upcoming: "true" }],
  ])("%s", async (shortcut, listed, notListed, params) => {
    const api = mockApi({
      [`GET ${ME}/activity-summary`]: { status: 200, body: { ...SUMMARY, meetings_today: 1, upcoming_meetings: 1 } },
      [`GET ${LIST}`]: listRoute,
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    const shortcuts = await screen.findByRole("navigation", { name: "Shortcuts" });
    await user.click(await within(shortcuts).findByRole("button", { name: new RegExp(`1\\s*${shortcut}`) }));
    const table = await screen.findByRole("table", { name: "Activities" });
    await within(table).findByText(listed);
    expect(within(table).queryByText(notListed)).not.toBeInTheDocument();
    const query = api.callsTo("GET", LIST).at(-1)!.query;
    expect({ cancelled: query.get("cancelled"), status: query.get("status"), upcoming: query.get("upcoming") }).toEqual(params);
  });
});

describe("controls have names that tell them apart (R8, P3)", () => {
  it("each note's row menu names its note; the page's create buttons say what they create", async () => {
    mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: {
        status: 200,
        body: page([
          asListItem(makeNote({ id: "a1c1e000-0000-4000-8000-0000000000c1", description: "Prefers morning calls." })),
          asListItem(makeNote({ id: "a1c1e000-0000-4000-8000-0000000000c2", description: "Budget approved in Q3." })),
        ]),
      },
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const table = await screen.findByRole("table", { name: "Activities" });
    expect(within(table).getByRole("button", { name: "Actions for note: Prefers morning calls." })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Actions for note: Budget approved in Q3." })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New task" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New meeting" })).toBeInTheDocument();
  });
});

describe("a meeting that has started is offered Complete without a reload (R9, P3)", () => {
  it("even when it was fetched before it started", async () => {
    nav.pathname = `/activities/${MEETING_ID}`;
    const startedFiveMinutesAgo = new Date(Date.now() - 5 * 60_000).toISOString();
    const endsLater = new Date(Date.now() + 25 * 60_000).toISOString();
    mockApi({
      [`GET ${activityUrl(MEETING_ID)}`]: { status: 200, body: makeMeeting({ starts_at: startedFiveMinutesAgo, ends_at: endsLater, completable: false }) },
    });
    renderWithProviders(<ActivityView activityId={MEETING_ID} />, { viewer: SALES });
    await screen.findByRole("heading", { name: "Product demo" });
    expect(screen.getByRole("button", { name: "Mark as completed" })).toBeInTheDocument();
    expect(screen.queryByText(/can be marked as completed once it has started/)).not.toBeInTheDocument();
  });
});

describe("the task/meeting dialog asks before discarding typing (R10, P3)", () => {
  it("Escape with unsaved input asks; Keep editing keeps it; Discard closes", async () => {
    mockApi({ ...SUMMARY_ROUTE, [`GET ${LIST}`]: { status: 200, body: page([]) }, [`GET ${ME}/leads`]: { status: 200, body: page([]) } });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    await screen.findByText("No activities yet");
    await user.click(screen.getByRole("button", { name: "New meeting" }));
    const dialog = screen.getByRole("dialog", { name: "Schedule a meeting" });
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Quarterly review");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Schedule a meeting" })).toBeInTheDocument();
    expect(within(dialog).getByText("Discard what you typed?")).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Keep editing" })).toHaveFocus());
    await user.click(within(dialog).getByRole("button", { name: "Keep editing" }));
    expect(within(dialog).getByRole("textbox", { name: "Subject" })).toHaveValue("Quarterly review");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await user.click(within(dialog).getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("dialog", { name: "Schedule a meeting" })).not.toBeInTheDocument();
  });

  it("an untouched dialog closes at once", async () => {
    mockApi({ ...SUMMARY_ROUTE, [`GET ${LIST}`]: { status: 200, body: page([]) }, [`GET ${ME}/leads`]: { status: 200, body: page([]) } });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const user = userEvent.setup();
    await screen.findByText("No activities yet");
    await user.click(screen.getByRole("button", { name: "New task" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "New task" })).not.toBeInTheDocument();
  });
});

describe("rescheduling a meeting moves its end too (R11, P3)", () => {
  it("moving the start later the same day keeps the length and saves", async () => {
    nav.pathname = `/activities/${MEETING_ID}`;
    const api = mockApi({
      [`GET ${activityUrl(MEETING_ID)}`]: { status: 200, body: makeMeeting() }, // 11:00-12:00 IST on 6 Oct
      [`PATCH ${activityUrl(MEETING_ID)}`]: (call: RecordedCall) => ({ status: 200, body: makeMeeting({ ...(call.body as Partial<Activity>), version: 2 }) }),
    });
    renderWithProviders(<ActivityView activityId={MEETING_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog", { name: "Edit meeting" });
    fireEvent.change(within(dialog).getByLabelText("Start"), { target: { value: "2026-10-06T15:00" } });
    expect(within(dialog).getByLabelText("End")).toHaveValue("2026-10-06T16:00");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.callsTo("PATCH", activityUrl(MEETING_ID))).toHaveLength(1));
    expect(api.callsTo("PATCH", activityUrl(MEETING_ID))[0]!.body).toEqual({
      version: 1,
      starts_at: "2026-10-06T09:30:00.000Z",
      ends_at: "2026-10-06T10:30:00.000Z",
    });
  });
});

describe("a reassignment out of the viewed workspace (live walkthrough)", () => {
  it("doesn't refetch the lead's timeline or open work where the lead no longer is", async () => {
    const RAHUL = `/api/v1/workspaces/${RAHUL_ID}`;
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    const api = mockApi({
      "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS },
      [`GET ${RAHUL}/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      [`GET ${RAHUL}/leads/${LEAD_ID}/timeline`]: { status: 200, body: page([makeEntry()]) },
      [`GET ${RAHUL}/activities`]: { status: 200, body: page([]) },
      "GET /api/v1/assignees": {
        status: 200,
        body: { results: [{ id: PRIYA_ID, full_name: "Priya Patel", email: "priya@example.test" }], next: null, previous: null },
      },
      [`POST ${RAHUL}/leads/${LEAD_ID}/assign`]: {
        status: 200,
        body: makeLead({ owner: { id: PRIYA_ID, full_name: "Priya Patel", is_active: true }, version: 4 }),
      },
    });
    const view = renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await screen.findByText(/Lead created/);
    await waitFor(() => expect(api.callsTo("GET", `${RAHUL}/activities`)).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Reassign" }));
    const dialog = screen.getByRole("dialog", { name: "Reassign Asha Mehta" });
    const owner = within(dialog).getByRole("combobox", { name: "New owner" });
    await waitFor(() => expect(owner).toBeEnabled());
    await user.selectOptions(owner, PRIYA_ID);
    await user.click(within(dialog).getByRole("button", { name: "Reassign lead" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/admin/users/${RAHUL_ID}/leads`));
    await new Promise((resolve) => setTimeout(resolve, 50));
    // Each was requested once, before the reassignment: the timeline would now be a 404.
    expect(api.callsTo("GET", `${RAHUL}/leads/${LEAD_ID}/timeline`)).toHaveLength(1);
    expect(api.callsTo("GET", `${RAHUL}/activities`)).toHaveLength(1);
    expect(screen.queryByText(/timeline couldn't be loaded/)).not.toBeInTheDocument();
    view.unmount();
    expect(view.client.getQueryData(["timeline", "lead", RAHUL_ID, LEAD_ID])).toBeUndefined();
    expect(view.client.getQueryData(["activities", "current", RAHUL_ID, LEAD_ID, ""])).toBeUndefined();
  });
});
