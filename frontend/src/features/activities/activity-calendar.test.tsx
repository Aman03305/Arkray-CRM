import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ActivitiesView } from "@/features/workspace/views";
import type { ActivityListItem } from "@/lib/api/types";
import { asListItem, makeActivity, makeMeeting, page, SUMMARY } from "@/test/activity-fixtures";
import { adminViewer, LEAD_ID, makeLeadListItem, makeViewer, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

import { CALENDAR_MAX_PAGES } from "./api";
import { forgetActivityListState, presetActivityList } from "./list-state";

const nav = vi.hoisted(() => ({ pathname: "/activities", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const ME = "/api/v1/workspaces/me";
const ALL = "/api/v1/workspaces/all";
const LIST = `${ME}/activities`;
// Monday 5 October 2026, 12:00 in India.
const NOW = new Date("2026-10-05T06:30:00Z");

const TASK = asListItem(makeActivity({ id: "a1c1e000-0000-4000-8000-0000000000b1", title: "Send the revised quotation", due_at: "2026-10-05T12:30:00Z" })); // 18:00
const MEETING = asListItem(makeMeeting({ id: "a1c1e000-0000-4000-8000-0000000000b2", title: "Product demo", starts_at: "2026-10-07T05:30:00Z", ends_at: "2026-10-07T06:30:00Z" })); // 11:00-12:00

/** The calendar's reads (100 a page), apart from the list's. */
const calendarCalls = (calls: RecordedCall[], type?: string) =>
  calls.filter((c) => c.query.get("page_size") === "100" && (!type || c.query.get("type") === type));

function routes(base: string, entries: { task?: ActivityListItem[]; meeting?: ActivityListItem[] } = {}) {
  return {
    [`GET ${base}/activity-summary`]: { status: 200, body: SUMMARY },
    [`GET ${base}/activities`]: (call: RecordedCall) => {
      const type = call.query.get("type");
      if (call.query.get("page_size") !== "100") return { status: 200, body: page([]) };
      return { status: 200, body: page(type === "task" ? (entries.task ?? []) : type === "meeting" ? (entries.meeting ?? []) : []) };
    },
  };
}

async function openCalendar() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Calendar" }));
  return user;
}

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
  nav.pathname = "/activities";
  forgetActivityListState();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the Activities calendar", () => {
  it("shows this month's tasks and meetings on their days, read through the list API per type", async () => {
    const api = mockApi(routes(ME, { task: [TASK], meeting: [MEETING] }));
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    await openCalendar();

    expect(screen.getByRole("button", { name: "Calendar" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("heading", { name: "October 2026" })).toBeInTheDocument();
    const link = await screen.findByRole("link", { name: "Task: 6:00 pm Send the revised quotation" });
    expect(link).toHaveAttribute("href", `/activities/${TASK.id}`);
    expect(screen.getByRole("link", { name: "Meeting: 11:00 am Product demo" })).toBeInTheDocument();
    // Today is marked, and each day's button says how much is on it.
    const today = screen.getByRole("button", { name: "Monday, 5 October 2026, 1 activity. Open the day" });
    expect(today).toHaveAttribute("aria-current", "date");
    expect(within(link.closest("td")!).getByRole("button", { name: /^Monday, 5 October 2026/ })).toBe(today);

    // The whole grid (28 Sep - 1 Nov), cancelled and archived left out, soonest first.
    for (const type of ["task", "meeting"]) {
      const [call] = calendarCalls(api.calls, type);
      expect(Object.fromEntries(call!.query)).toEqual({
        type,
        cancelled: "false",
        date_from: "2026-09-28",
        date_to: "2026-11-01",
        ordering: "scheduled",
        page_size: "100",
      });
    }
    // A salesperson's own calendar has no "whose" switch.
    expect(screen.queryByRole("group", { name: "Whose activities" })).not.toBeInTheDocument();
  });

  it("follows the next pages and says when a range is cut short", async () => {
    const api = mockApi({
      ...routes(ME),
      [`GET ${LIST}`]: (call: RecordedCall) => {
        if (call.query.get("type") !== "task") return { status: 200, body: page([]) };
        const n = Number(call.query.get("cursor") ?? "0");
        return { status: 200, body: page([asListItem(makeActivity({ id: `task-${n}`, title: `Task ${n}` }))], `${LIST}?cursor=${n + 1}`) };
      },
    });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    await openCalendar();

    expect(await screen.findByText("Not everything fits")).toBeInTheDocument();
    const tasks = calendarCalls(api.calls, "task");
    expect(tasks).toHaveLength(CALENDAR_MAX_PAGES);
    expect(tasks.map((c) => c.query.get("cursor"))).toEqual([null, "1", "2", "3", "4"]);
    // Every page repeats the first one's parameters (cursors are bound to them).
    for (const call of tasks.slice(1)) {
      const query = new URLSearchParams(call.query);
      query.delete("cursor");
      expect(query.toString()).toBe(tasks[0]!.query.toString());
    }
  });

  it("moves by month, week and day, and Today comes back", async () => {
    const api = mockApi(routes(ME, { task: [TASK] }));
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = await openCalendar();
    const lastRange = () => {
      const call = calendarCalls(api.calls, "task").at(-1)!;
      return [call.query.get("date_from"), call.query.get("date_to")];
    };

    await user.click(screen.getByRole("button", { name: "Next month" }));
    expect(screen.getByRole("heading", { name: "November 2026" })).toBeInTheDocument();
    await waitFor(() => expect(lastRange()).toEqual(["2026-10-26", "2026-12-06"]));

    await user.click(screen.getByRole("button", { name: "Week" }));
    expect(screen.getByRole("heading", { name: "26 Oct – 1 Nov 2026" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Today" }));
    expect(screen.getByRole("heading", { name: "5 – 11 Oct 2026" })).toBeInTheDocument();
    await waitFor(() => expect(lastRange()).toEqual(["2026-10-05", "2026-10-11"]));

    await user.click(screen.getByRole("button", { name: "Day" }));
    expect(screen.getByRole("heading", { name: "Monday, 5 October 2026" })).toBeInTheDocument();
    await waitFor(() => expect(lastRange()).toEqual(["2026-10-05", "2026-10-05"]));
    await user.click(screen.getByRole("button", { name: "Previous day" }));
    expect(screen.getByRole("heading", { name: "Sunday, 4 October 2026" })).toBeInTheDocument();
  });

  it("a busy day lists a few and opens the day for the rest", async () => {
    const busy = [9, 10, 11, 12].map((hour) =>
      asListItem(makeActivity({ id: `busy-${hour}`, title: `Call ${hour}`, due_at: new Date(Date.UTC(2026, 9, 8, hour - 5, 30)).toISOString() })),
    );
    mockApi(routes(ME, { task: busy }));
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = await openCalendar();

    await user.click(await screen.findByRole("button", { name: "2 more on Thursday, 8 October 2026" }));
    expect(screen.getByRole("heading", { name: "Thursday, 8 October 2026" })).toBeInTheDocument();
    const day = await screen.findByRole("region", { name: "Thursday, 8 October 2026, 4 activities" });
    expect(within(day).getAllByRole("link")).toHaveLength(4);
  });

  it("week view places entries by time and length; a short one is a single line", async () => {
    const callBack = asListItem(makeActivity({ id: "a1c1e000-0000-4000-8000-0000000000b3", title: "Call back", due_at: "2026-10-07T09:30:00Z" })); // 15:00
    mockApi(routes(ME, { task: [callBack], meeting: [MEETING] }));
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = await openCalendar();
    await user.click(screen.getByRole("button", { name: "Week" }));

    const day = await screen.findByRole("region", { name: "Wednesday, 7 October 2026, 2 activities" });
    const block = within(day).getByRole("link", { name: "Meeting: Product demo, 11:00 am – 12:00 pm" });
    expect(block.closest("li")).toHaveStyle({ top: `${11 * 48}px`, height: "48px" });
    // 30 minutes (24 px) has room for one line only (three squeezed lines were unreadable).
    const task = within(day).getByRole("link", { name: "Task: 3:00 pm Call back" });
    expect(task.closest("li")).toHaveStyle({ top: `${15 * 48}px`, height: "24px" });
  });

  it("choosing a day opens a prefilled form that can schedule a meeting or a task", async () => {
    const api = mockApi({
      ...routes(ME),
      [`GET ${ME}/leads`]: { status: 200, body: page([makeLeadListItem()]) },
      [`GET ${ME}/opportunities`]: { status: 200, body: page([]) },
      [`POST ${LIST}`]: { status: 201, body: makeActivity({ title: "Call back", due_at: "2026-10-07T12:30:00Z" }) },
    });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = await openCalendar();

    await user.click(screen.getByRole("button", { name: "Schedule on Wednesday, 7 October 2026" }));
    let dialog = screen.getByRole("dialog", { name: "Schedule a meeting" });
    expect(within(dialog).getByLabelText("Start")).toHaveValue("2026-10-07T09:00");
    expect(within(dialog).getByLabelText("End")).toHaveValue("2026-10-07T09:30");

    // Switching keeps what was typed; the task is due that day.
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Call back");
    await user.click(within(dialog).getByRole("button", { name: "Task" }));
    dialog = screen.getByRole("dialog", { name: "New task" });
    expect(within(dialog).getByRole("textbox", { name: "Subject" })).toHaveValue("Call back");
    expect(within(dialog).getByLabelText(/^Due date/)).toHaveValue("2026-10-07");
    expect(within(dialog).getByLabelText("Due time")).toHaveValue("18:00");

    await within(dialog).findByRole("option", { name: /Asha Mehta/ });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Lead" }), LEAD_ID);
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(api.callsTo("POST", LIST)).toHaveLength(1));
    expect(api.callsTo("POST", LIST)[0]!.body).toEqual({
      type: "task",
      title: "Call back",
      lead: LEAD_ID,
      priority: "normal",
      due_at: "2026-10-07T12:30:00.000Z", // 18:00 in India
    });
    expect(await screen.findByText('Task "Call back" created.')).toBeInTheDocument();
    // The calendar reads its range again after the write.
    await waitFor(() => expect(calendarCalls(api.calls, "task").length).toBeGreaterThan(1));
  });

  it("an untouched prefilled form closes without asking", async () => {
    mockApi({ ...routes(ME), [`GET ${ME}/leads`]: { status: 200, body: page([]) } });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = await openCalendar();
    await user.click(screen.getByRole("button", { name: "Schedule on Wednesday, 7 October 2026" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("clicking a time in the week view starts the meeting then", async () => {
    mockApi({ ...routes(ME), [`GET ${ME}/leads`]: { status: 200, body: page([]) } });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = await openCalendar();
    await user.click(screen.getByRole("button", { name: "Week" }));

    // jsdom lays nothing out: the column's top is 0, so clientY is the offset (48 px an hour).
    fireEvent.click(screen.getByRole("region", { name: /^Wednesday, 7 October 2026/ }), { clientY: 14.6 * 48 });
    const dialog = screen.getByRole("dialog", { name: "Schedule a meeting" });
    expect(within(dialog).getByLabelText("Start")).toHaveValue("2026-10-07T14:30");
    expect(within(dialog).getByLabelText("End")).toHaveValue("2026-10-07T15:00");
  });

  it("organisation-wide: everyone's by default with owners named; My calendar narrows to the viewer's", async () => {
    const api = mockApi(routes(ALL, { task: [TASK] }));
    renderWithProviders(<ActivitiesView />, { viewer: adminViewer });
    const user = await openCalendar();

    expect(await screen.findByRole("link", { name: "Task, owner Rahul Sharma: 6:00 pm Send the revised quotation" })).toBeInTheDocument();
    const whose = screen.getByRole("group", { name: "Whose activities" });
    expect(within(whose).getByRole("button", { name: "Everyone" })).toHaveAttribute("aria-pressed", "true");
    expect(calendarCalls(api.calls, "task")[0]!.query.get("owner")).toBeNull();

    await user.click(within(whose).getByRole("button", { name: "My calendar" }));
    await waitFor(() => expect(calendarCalls(api.calls, "task").at(-1)!.query.get("owner")).toBe(adminViewer.id));
    expect(RAHUL_ID).not.toBe(adminViewer.id);
  });

  it("without write access there is nothing to schedule", async () => {
    mockApi(routes(ALL, { task: [TASK] }));
    renderWithProviders(<ActivitiesView />, { viewer: makeViewer({ capabilities: ["crm.access_own", "crm.view_all"] }) });
    await openCalendar();
    await screen.findByRole("link", { name: /Send the revised quotation/ });
    expect(screen.queryByRole("button", { name: /^Schedule on/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Wednesday, 7 October 2026/ }).closest("td")!);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("the calendar is remembered for the workspace; a shortcut or a dashboard figure shows the list", async () => {
    mockApi(routes(ME, { task: [TASK] }));
    const first = renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    const user = await openCalendar();
    await user.click(screen.getByRole("button", { name: "Week" }));
    first.unmount();

    const second = renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "5 – 11 Oct 2026" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Overdue tasks/ }));
    expect(screen.getByRole("button", { name: "Tasks" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("group", { name: "Calendar view" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Calendar" }));
    second.unmount();

    presetActivityList("me", { tab: "meeting" });
    renderWithProviders(<ActivitiesView />, { viewer: salesViewer });
    expect(await screen.findByRole("button", { name: "Meetings" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("group", { name: "Calendar view" })).not.toBeInTheDocument();
  });
});
