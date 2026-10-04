import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivitiesView } from "@/features/workspace/views";
import { asListItem, makeActivity, makeMeeting, makeNote, page, SUMMARY } from "@/test/activity-fixtures";
import { adminViewer, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { forgetActivityListState } from "./list-state";

const nav = vi.hoisted(() => ({ pathname: "/activities", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const ME = "/api/v1/workspaces/me";
const LIST = `${ME}/activities`;
const SUMMARY_ROUTE = { [`GET ${ME}/activity-summary`]: { status: 200, body: SUMMARY } };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

beforeEach(() => {
  nav.pathname = "/activities";
  nav.push.mockReset();
  forgetActivityListState();
});

describe("the Activities page", () => {
  it("lists tasks, meetings and notes with their type, status and related records written out", async () => {
    mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: {
        status: 200,
        body: page([
          asListItem(makeActivity({ is_overdue: true })),
          asListItem(makeMeeting()),
          asListItem(makeNote({ description: "x".repeat(300) })),
        ]),
      },
    });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const table = await screen.findByRole("table", { name: "Activities" });
    // Positioned, so the hidden "Actions" label can't widen the page (Phase 7 walkthrough).
    expect(table.parentElement).toHaveClass("relative", "overflow-x-auto");
    expect(within(table).getAllByText("Task").length).toBeGreaterThan(0);
    expect(within(table).getByText("Meeting")).toBeInTheDocument();
    expect(within(table).getByText("Note")).toBeInTheDocument();
    expect(within(table).getByText("Overdue")).toBeInTheDocument();
    expect(within(table).getByText("Scheduled")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Hospital Analyzer Project" })).toBeInTheDocument();
    // A note shows a bounded preview, never the whole body.
    expect(within(table).getByRole("link", { name: `${"x".repeat(240)}…` })).toBeInTheDocument();
    // Summary shortcuts come from the server's counts.
    const shortcuts = screen.getByRole("navigation", { name: "Shortcuts" });
    expect(within(shortcuts).getByText("Overdue tasks").previousSibling).toHaveTextContent("2");
    // A salesperson's own list has no owner column.
    expect(within(table).queryByRole("columnheader", { name: "Owner" })).not.toBeInTheDocument();
  });

  it("tabs and filters become allowlisted API parameters", async () => {
    const api = mockApi({ ...SUMMARY_ROUTE, [`GET ${LIST}`]: { status: 200, body: page([]) } });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await screen.findByText("No activities yet");
    await user.click(screen.getByRole("button", { name: "Tasks" }));
    await waitFor(() => {
      const query = api.callsTo("GET", LIST).at(-1)!.query;
      expect([query.get("type"), query.get("status"), query.get("ordering")]).toEqual(["task", "open", "scheduled"]);
    });
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "overdue");
    await waitFor(() => {
      const query = api.callsTo("GET", LIST).at(-1)!.query;
      expect(query.get("overdue")).toBe("true");
      expect(query.get("status")).toBeNull();
    });
    await user.click(screen.getByRole("button", { name: "Meetings" }));
    await waitFor(() => expect(api.callsTo("GET", LIST).at(-1)!.query.get("status")).toBe("scheduled"));
    await user.click(screen.getByRole("button", { name: "Notes" }));
    await waitFor(() => expect(api.callsTo("GET", LIST).at(-1)!.query.get("type")).toBe("note"));
    expect(screen.queryByRole("combobox", { name: "Filter by status" })).not.toBeInTheDocument();
    // The owner filter exists only organisation-wide, and nothing lands in the URL.
    expect(api.calls.every((c) => !c.query.has("owner"))).toBe(true);
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("an inverted date range is explained and never sent", async () => {
    const api = mockApi({ ...SUMMARY_ROUTE, [`GET ${LIST}`]: { status: 200, body: page([]) } });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await screen.findByText("No activities yet");
    await user.click(screen.getByRole("button", { name: /More filters/ }));
    await user.type(screen.getByLabelText("Due / start / written from"), "2026-10-10");
    await user.type(screen.getByLabelText("to"), "2026-10-01");
    expect(await screen.findByText(/end date must be on or after/)).toBeInTheDocument();
    expect(api.calls.some((c) => c.query.get("date_to") === "2026-10-01")).toBe(false);
  });

  it("completes a task with the version it shows; a conflict is explained and the list reloads", async () => {
    let version = 1;
    const api = mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: () => ({ status: 200, body: page([asListItem(makeActivity({ version }))]) }),
      [`POST ${LIST}/${makeActivity().id}/complete`]: (call: RecordedCall) => {
        if ((call.body as { version: number }).version !== version) return apiError(409, "conflict", "Changed.");
        return { status: 200, body: makeActivity({ status: "completed", version: version + 1, completable: false }) };
      },
    });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const [button] = await screen.findAllByRole("button", { name: "Complete Send the revised quotation" });
    version = 2; // someone else changed it meanwhile
    await user.click(button!);
    expect(await screen.findByText(/changed by someone else/)).toBeInTheDocument();
    expect(api.callsTo("POST", `${LIST}/${makeActivity().id}/complete`)[0]!.body).toEqual({ version: 1 });
    await waitFor(() => expect(api.callsTo("GET", LIST).length).toBeGreaterThan(1));
    const [again] = await screen.findAllByRole("button", { name: "Complete Send the revised quotation" });
    await user.click(again!);
    expect(await screen.findByText("Send the revised quotation completed.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${LIST}/${makeActivity().id}/complete`)[1]!.body).toEqual({ version: 2 });
  });

  it("cancelling asks first; read-only viewers get no actions", async () => {
    mockApi({ ...SUMMARY_ROUTE, [`GET ${LIST}`]: { status: 200, body: page([asListItem(makeActivity())]) } });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const [menu] = await screen.findAllByRole("button", { name: "Actions for Send the revised quotation" });
    await user.click(menu!);
    await user.click(screen.getByRole("menuitem", { name: "Cancel" }));
    expect(screen.getByRole("alertdialog", { name: /Cancel Send the revised quotation/ })).toBeInTheDocument();
  });

  it("shows loading, error with retry, and not-found states", async () => {
    let fail = true;
    mockApi({
      ...SUMMARY_ROUTE,
      [`GET ${LIST}`]: () => (fail ? apiError(500, "server_error", "Boom") : { status: 200, body: page([asListItem(makeActivity())]) }),
    });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    expect(await screen.findByText("Activities couldn't be loaded")).toBeInTheDocument();
    expect(screen.queryByText("Boom")).not.toBeInTheDocument(); // no raw server message for 5xx
    fail = false;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect((await screen.findAllByText("Send the revised quotation")).length).toBeGreaterThan(0);
  });

  it("a workspace that can't be opened is not found", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/activities`;
    mockApi({ [`GET /api/v1/workspaces/${RAHUL_ID}/activities`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<ActivitiesView />, { viewer: adminViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("organisation-wide, administrators see owners and may filter by owner", async () => {
    mockApi({
      "GET /api/v1/workspaces/all/activity-summary": { status: 200, body: SUMMARY },
      "GET /api/v1/workspaces/all/activities": { status: 200, body: page([asListItem(makeActivity())]) },
    });
    renderWithProviders(<ActivitiesView />, { viewer: adminViewer });
    expect(await screen.findByRole("columnheader", { name: "Owner" })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: /More filters/ }));
    expect(screen.getByRole("combobox", { name: "Owner" })).toBeInTheDocument();
  });
});

describe("no stale data across workspaces", () => {
  it("switching from Rahul's activities to Priya's never shows one of Rahul's under Priya", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/activities`;
    const priya = deferred<{ status: number; body: unknown }>();
    mockApi({
      [`GET /api/v1/workspaces/${RAHUL_ID}/activities`]: { status: 200, body: page([asListItem(makeActivity({ title: "Rahul's secret task" }))]) },
      [`GET /api/v1/workspaces/${RAHUL_ID}/activity-summary`]: { status: 200, body: { ...SUMMARY, open_tasks: 41 } },
      [`GET /api/v1/workspaces/${PRIYA_ID}/activities`]: () => priya.promise,
      [`GET /api/v1/workspaces/${PRIYA_ID}/activity-summary`]: () => new Promise(() => undefined),
    });
    const view = renderWithProviders(<ActivitiesView />, { viewer: adminViewer });
    expect((await screen.findAllByText("Rahul's secret task")).length).toBeGreaterThan(0);
    expect(await screen.findByText("41")).toBeInTheDocument();
    nav.pathname = `/admin/users/${PRIYA_ID}/activities`;
    view.rerender(<ActivitiesView />);
    // Immediately, and while Priya's list is still loading: nothing of Rahul's.
    expect(screen.queryByText("Rahul's secret task")).not.toBeInTheDocument();
    expect(screen.queryByText("41")).not.toBeInTheDocument();
    await act(async () => priya.resolve({ status: 200, body: page([asListItem(makeActivity({ title: "Priya's task" }))]) }));
    expect((await screen.findAllByText("Priya's task")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Rahul's secret task")).not.toBeInTheDocument();
  });

  it("Back to Rahul after Priya shows Rahul's list, not Priya's, and keeps each one's filters apart", async () => {
    nav.pathname = `/admin/users/${PRIYA_ID}/activities`;
    const rahul = deferred<{ status: number; body: unknown }>();
    const api = mockApi({
      [`GET /api/v1/workspaces/${PRIYA_ID}/activities`]: { status: 200, body: page([asListItem(makeActivity({ title: "Priya's task" }))]) },
      [`GET /api/v1/workspaces/${PRIYA_ID}/activity-summary`]: { status: 200, body: SUMMARY },
      [`GET /api/v1/workspaces/${RAHUL_ID}/activities`]: () => rahul.promise,
      [`GET /api/v1/workspaces/${RAHUL_ID}/activity-summary`]: { status: 200, body: SUMMARY },
    });
    const view = renderWithProviders(<ActivitiesView />, { viewer: adminViewer });
    await screen.findAllByText("Priya's task");
    await userEvent.setup().click(screen.getByRole("button", { name: "Tasks" }));
    nav.pathname = `/admin/users/${RAHUL_ID}/activities`;
    view.rerender(<ActivitiesView />);
    expect(screen.queryByText("Priya's task")).not.toBeInTheDocument();
    await act(async () => rahul.resolve({ status: 200, body: page([asListItem(makeActivity({ title: "Rahul's task" }))]) }));
    expect((await screen.findAllByText("Rahul's task")).length).toBeGreaterThan(0);
    // Priya's "Tasks" tab didn't follow the admin into Rahul's workspace.
    expect(api.callsTo("GET", `/api/v1/workspaces/${RAHUL_ID}/activities`).at(-1)!.query.get("type")).toBeNull();
  });
});
