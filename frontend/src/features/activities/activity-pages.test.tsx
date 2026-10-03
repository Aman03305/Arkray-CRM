import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityView, LeadView } from "@/features/workspace/views";
import type { Activity } from "@/lib/api/types";
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
  TASK_ID,
} from "@/test/activity-fixtures";
import { adminViewer, LEAD_ID, LEAD_OPTIONS, makeLead, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/activities/x", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const ME = "/api/v1/workspaces/me";
const activityUrl = (id: string, action = "") => `${ME}/activities/${id}${action ? `/${action}` : ""}`;
const SALES = { ...salesViewer, id: RAHUL_ID };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

beforeEach(() => {
  nav.pathname = `/activities/${TASK_ID}`;
  nav.push.mockReset();
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

describe("the task form", () => {
  it("focuses the first problem, then creates the task for the lead with an idempotency key reused on retry", async () => {
    nav.pathname = `/leads/${LEAD_ID}`;
    let attempt = 0;
    const api = mockApi({
      "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS },
      [`GET ${ME}/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      [`GET ${ME}/leads/${LEAD_ID}/timeline`]: { status: 200, body: page([]) },
      [`GET ${ME}/activities`]: { status: 200, body: page([]) },
      [`POST ${ME}/activities`]: () => {
        attempt += 1;
        return attempt === 1 ? apiError(503, "service_unavailable", "Try again.") : { status: 201, body: makeActivity({ title: "Call back" }) };
      },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    const openWork = await screen.findByRole("region", { name: "Open work" });
    await user.click(within(openWork).getByRole("button", { name: "New task" }));
    const dialog = screen.getByRole("dialog", { name: "New task" });
    expect(within(dialog).getByText("Asha Mehta")).toBeInTheDocument(); // fixed: the lead's page
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    expect(within(dialog).getByRole("textbox", { name: "Subject" })).toHaveFocus();
    expect(within(dialog).getByText("Enter a subject.")).toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Call back");
    await user.type(within(dialog).getByLabelText(/Due date/), "2026-10-03");
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    expect(await within(openWork).findByText("Task created.")).toBeInTheDocument();
    const posts = api.callsTo("POST", `${ME}/activities`);
    expect(posts).toHaveLength(2);
    expect(posts[0]!.body).toEqual({ type: "task", title: "Call back", lead: LEAD_ID, priority: "normal", due_at: "2026-10-03T12:30:00.000Z" });
    expect(posts[0]!.headers["Idempotency-Key"]).toBe(posts[1]!.headers["Idempotency-Key"]);
  });

  it("an edit conflict keeps the typing and offers to apply it to the latest version", async () => {
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

describe("the timeline", () => {
  beforeEach(() => {
    nav.pathname = `/leads/${LEAD_ID}`;
  });

  function leadRoutes(timeline: unknown, extra: Record<string, unknown> = {}) {
    return {
      "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS },
      [`GET ${ME}/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      [`GET ${ME}/activities`]: { status: 200, body: page([]) },
      [`GET ${ME}/leads/${LEAD_ID}/timeline`]: timeline,
      ...extra,
    } as Parameters<typeof mockApi>[0];
  }

  it("tells the lead's story in words, newest first, with restricted records unnamed", async () => {
    mockApi(
      leadRoutes({
        status: 200,
        body: page([
          noteEntry("y".repeat(300), { id: 9 }),
          makeEntry({
            id: 8,
            kind: "task.created",
            activity: { id: TASK_ID, type: "task", title: "Follow up", preview: "", preview_truncated: false, status: "open", due_at: null, starts_at: null, ends_at: null },
            opportunity: { id: null, restricted: true },
          }),
          makeEntry({
            id: 7,
            kind: "lead.reassigned",
            actor: { id: "a1", full_name: "Anita Admin", is_active: true },
            details: { from_owner: { id: PRIYA_ID, full_name: "Priya Patel", is_active: false }, to_owner: SALES_REF },
          }),
          makeEntry({ id: 6, kind: "lead.status_changed", details: { from: "new", from_name: "New", to: "contacted", to_name: "Contacted" } }),
        ]),
      }),
    );
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: SALES });
    const timeline = await screen.findByRole("region", { name: "Timeline" });
    const items = await within(timeline).findAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(items[0]).toHaveTextContent(`${"y".repeat(240)}…`);
    expect(within(items[0]!).getByRole("link", { name: "Read the whole note" })).toHaveAttribute("href", `/activities/${NOTE_ID}`);
    expect(items[1]).toHaveTextContent("Task created: Follow up");
    expect(items[1]).toHaveTextContent("an opportunity in another workspace");
    expect(items[2]).toHaveTextContent(/Reassigned from Priya Patel\s*\(deactivated\) to Rahul Sharma/);
    expect(items[2]).toHaveTextContent("Anita Admin");
    expect(items[3]).toHaveTextContent("Status changed from New to Contacted");
    expect(within(timeline).getAllByRole("listitem").every((li) => li.querySelector("time"))).toBe(true);
  });

  it("shows older entries on request with the server's cursor", async () => {
    const api = mockApi(
      leadRoutes((call: RecordedCall) =>
        call.query.get("cursor")
          ? { status: 200, body: page([makeEntry({ id: 1, kind: "lead.created" })]) }
          : { status: 200, body: page([noteEntry("Newest", { id: 5 })], `http://testserver${ME}/leads/${LEAD_ID}/timeline?cursor=abc`) },
      ),
    );
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: SALES });
    const timeline = await screen.findByRole("region", { name: "Timeline" });
    await userEvent.setup().click(await within(timeline).findByRole("button", { name: "Show older" }));
    expect(await within(timeline).findByText(/Lead created/)).toBeInTheDocument();
    expect(within(timeline).getByText("Newest")).toBeInTheDocument();
    expect(api.callsTo("GET", `${ME}/leads/${LEAD_ID}/timeline`).at(-1)!.query.get("cursor")).toBe("abc");
  });

  it("adds a note in two steps; a failure keeps the text, and the retry reuses its key", async () => {
    let attempt = 0;
    const api = mockApi(
      leadRoutes(
        { status: 200, body: page([]) },
        {
          [`POST ${ME}/activities`]: () => {
            attempt += 1;
            return attempt === 1 ? apiError(400, "validation_error", "Some fields are invalid.", { description: ["Remove the invisible or control characters from this text."] }) : { status: 201, body: makeNote() };
          },
        },
      ),
    );
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Add a note" });
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(screen.getByText("Write the note first.")).toBeInTheDocument();
    await user.type(box, "Prefers morning calls.");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByText("Remove the invisible or control characters from this text.")).toBeInTheDocument();
    expect(box).toHaveValue("Prefers morning calls.");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByText("Note saved.")).toBeInTheDocument();
    expect(box).toHaveValue("");
    const posts = api.callsTo("POST", `${ME}/activities`);
    expect(posts[1]!.body).toEqual({ type: "note", lead: LEAD_ID, description: "Prefers morning calls." });
    expect(posts[0]!.headers["Idempotency-Key"]).toBe(posts[1]!.headers["Idempotency-Key"]);
  });

  it("a refused refetch hides entries instead of leaving them on screen", async () => {
    let refuse = false;
    mockApi(leadRoutes(() => (refuse ? apiError(404, "not_found", "Not found.") : { status: 200, body: page([noteEntry("Secret history")]) })));
    const { client } = renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: SALES });
    expect(await screen.findByText("Secret history")).toBeInTheDocument();
    refuse = true;
    await act(async () => {
      await client.refetchQueries({ queryKey: ["timeline"] });
    });
    await waitFor(() => expect(screen.queryByText("Secret history")).not.toBeInTheDocument());
  });

  it("Rahul's lead timeline never flashes on Priya's lead page", async () => {
    const PRIYA_LEAD = "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2d";
    const priyaTimeline = deferred<{ status: number; body: unknown }>();
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    mockApi({
      "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS },
      [`GET /api/v1/workspaces/${RAHUL_ID}/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      [`GET /api/v1/workspaces/${RAHUL_ID}/activities`]: { status: 200, body: page([asListItem(makeActivity({ title: "Rahul's task" }))]) },
      [`GET /api/v1/workspaces/${RAHUL_ID}/leads/${LEAD_ID}/timeline`]: { status: 200, body: page([noteEntry("Rahul's private note")]) },
      [`GET /api/v1/workspaces/${PRIYA_ID}/leads/${PRIYA_LEAD}`]: { status: 200, body: makeLead({ id: PRIYA_LEAD, display_name: "Priya's lead" }) },
      [`GET /api/v1/workspaces/${PRIYA_ID}/activities`]: { status: 200, body: page([]) },
      [`GET /api/v1/workspaces/${PRIYA_ID}/leads/${PRIYA_LEAD}/timeline`]: () => priyaTimeline.promise,
    });
    const view = renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    expect(await screen.findByText("Rahul's private note")).toBeInTheDocument();
    expect(await screen.findByText("Rahul's task")).toBeInTheDocument();
    nav.pathname = `/admin/users/${PRIYA_ID}/leads/${PRIYA_LEAD}`;
    view.rerender(<LeadView leadId={PRIYA_LEAD} />);
    expect(screen.queryByText("Rahul's private note")).not.toBeInTheDocument();
    expect(screen.queryByText("Rahul's task")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 1, name: "Priya's lead" })).toBeInTheDocument();
    expect(screen.queryByText("Rahul's private note")).not.toBeInTheDocument();
    await act(async () => priyaTimeline.resolve({ status: 200, body: page([noteEntry("Priya's note")]) }));
    expect(await screen.findByText("Priya's note")).toBeInTheDocument();
  });
});
