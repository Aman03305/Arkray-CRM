import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { AnchorHTMLAttributes, MouseEvent } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetActivityListState, useActivityListState } from "@/features/activities/list-state";
import { forgetLeadListState, useLeadListState } from "@/features/leads/list-state";
import { NO_BOARD_FILTERS } from "@/features/pipeline/api";
import { forgetBoardState, useBoardState } from "@/features/pipeline/hooks";
import { DashboardView } from "@/features/workspace/views";
import type { Dashboard } from "@/lib/api/types";
import {
  EMPTY_DASHBOARD,
  makeDashboard,
  makeDashboardLead,
  PRIYA,
  PRIYA_DASHBOARD,
  RAHUL_DASHBOARD,
  RAHUL_ONLY,
} from "@/test/dashboard-fixtures";
import { adminViewer, makeViewer, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { apiError, createTestQueryClient, mockApi, renderWithProviders } from "@/test/render";

import { WorkspaceDashboard } from "./DashboardView";

const navigation = vi.hoisted(() => ({ pathname: "/dashboard" }));
vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));
// A plain anchor that behaves like next/link: onClick always runs; onNavigate only for a
// navigation in this tab (an unmodified primary click), never for Ctrl/Cmd/Shift/Alt- or
// middle-clicks, which the browser opens elsewhere. Navigation itself is stopped (jsdom
// can't perform it).
vi.mock("next/link", () => ({
  default: ({
    href,
    onClick,
    onNavigate,
    children,
    ...rest
  }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; onNavigate?: (e: { preventDefault(): void }) => void }) => (
    <a
      href={href}
      onClick={(event: MouseEvent<HTMLAnchorElement>) => {
        onClick?.(event);
        const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0;
        if (!modified) onNavigate?.({ preventDefault: () => undefined });
        event.preventDefault();
      }}
      {...rest}
    >
      {children}
    </a>
  ),
}));

const ME = "/api/v1/workspaces/me/dashboard";
const ALL = "/api/v1/workspaces/all/dashboard";
const RAHUL_URL = `/api/v1/workspaces/${RAHUL_ID}/dashboard`;
const PRIYA_URL = `/api/v1/workspaces/${PRIYA_ID}/dashboard`;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const text = () => document.body.textContent ?? "";

beforeEach(() => {
  navigation.pathname = "/dashboard";
  forgetLeadListState();
  forgetActivityListState();
  forgetBoardState();
});

afterEach(() => {
  vi.useRealTimers();
  onlineManager.setOnline(true);
});

describe("the six figures", () => {
  it("show exactly what the server sent, labelled in words", async () => {
    mockApi({ [`GET ${ME}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });

    expect(await screen.findByRole("link", { name: /^Total leads\s+1,234$/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    for (const name of [
      /^New leads today\s+7$/,
      // Over every pipeline the workspace may see, and it says so.
      /^Pipeline value\s+₹15,00,000\s+2 open opportunities · all pipelines$/,
      /^Weighted pipeline\s+₹9,00,000\s+All pipelines$/,
      /^Meetings\s+3\s+today\s+6 upcoming$/,
      /^Tasks\s+9\s+open\s+2 due today · 1 overdue$/,
    ]) {
      expect(screen.getByRole("link", { name })).toBeInTheDocument();
    }
    expect(screen.getByText("3 Oct 2026")).toBeInTheDocument();
    for (const heading of ["Key figures", "New leads today", "Upcoming meetings", "Tasks requiring attention"]) {
      expect(screen.getByRole("heading", { level: 2, name: heading })).toBeInTheDocument();
    }
  });

  it("keep money exact: digits a JavaScript number would lose, paise, Indian grouping", async () => {
    mockApi({
      [`GET ${ME}`]: {
        status: 200,
        // 2^53 + 1 rupees: Number("9007199254740993.01") is 9007199254740992.
        body: makeDashboard({
          pipeline: { pipeline_value: "9007199254740993.01", weighted_pipeline: "0.30", open_count: 1 },
        }),
      },
    });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    const huge = await screen.findByText("₹9,00,71,99,25,47,40,993.01");
    expect(screen.getByText("₹0.30")).toBeInTheDocument();
    // It may wrap after a group's comma, never inside a group (review: a 15-digit total
    // broke as "…345.6" / "7" on a phone).
    expect(huge.querySelectorAll("wbr")).toHaveLength(7);
  });

  it("start from zero for a new user, with empty states that say so (no NaN, no blanks)", async () => {
    mockApi({ [`GET ${ME}`]: { status: 200, body: EMPTY_DASHBOARD } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });

    expect(await screen.findByRole("link", { name: /^Total leads\s+0$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Pipeline value\s+₹0\s+0 open opportunities · all pipelines$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Weighted pipeline\s+₹0\s/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Meetings\s+0\s+today\s+0 upcoming$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Tasks\s+0\s+open\s+0 due today · 0 overdue$/ })).toBeInTheDocument();
    expect(screen.getByText("No new leads yet today.")).toBeInTheDocument();
    expect(screen.getByText("No upcoming meetings.")).toBeInTheDocument();
    expect(screen.getByText("No open tasks.")).toBeInTheDocument();
    expect(text()).not.toMatch(/NaN|undefined|null|—/);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("loading and errors", () => {
  it("show a skeleton (announced), never zeros, while the figures load", async () => {
    const gate = deferred<{ status: number; body: Dashboard }>();
    mockApi({ [`GET ${ME}`]: () => gate.promise });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });

    expect(await screen.findByRole("status")).toHaveTextContent("Loading the dashboard");
    expect(text()).not.toMatch(/₹|\b0\b|Total leads/);
    await act(async () => gate.resolve({ status: 200, body: makeDashboard() }));
    expect(await screen.findByRole("link", { name: /Total leads/ })).toBeInTheDocument();
    expect(screen.queryByText("Loading the dashboard")).not.toBeInTheDocument();
    // The same live region stays in place (one inserted with its text already in it is often
    // not announced; review).
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("a refresh marks the figures as updating, then a failed one replaces them (review)", async () => {
    let call = 0;
    const second = deferred<{ status: number; body?: unknown }>();
    mockApi({ [`GET ${ME}`]: () => (++call === 1 ? { status: 200, body: makeDashboard() } : second.promise) });
    const { client } = renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(await screen.findByText("₹15,00,000")).toBeInTheDocument();
    act(() => void client.refetchQueries({ queryKey: ["dashboard"] }));
    expect(await screen.findByText("Updating…")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Updating the dashboard");
    expect(screen.getByRole("region", { name: "Key figures" })).toHaveAttribute("aria-busy", "true");
    await act(async () => second.resolve(apiError(503, "service_unavailable", "Down.")));
    expect(await screen.findByRole("alert")).toHaveTextContent("The dashboard couldn't be loaded");
    expect(text()).not.toContain("₹15,00,000");
  });

  it("offline, the request is still made and its failure explained (no endless skeleton; review)", async () => {
    onlineManager.setOnline(false);
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server");
  });

  it("refreshes once when the business day changes while the page stays open (review)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-10-03T18:20:00Z")); // 23:50 IST on 3 October
    const api = mockApi({ [`GET ${ME}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(await screen.findByText("₹15,00,000")).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTime(60_000)); // still 3 October in India
    expect(api.callsTo("GET", ME)).toHaveLength(1);
    vi.setSystemTime(new Date("2026-10-03T18:31:00Z")); // 00:01 IST on 4 October
    await act(async () => vi.advanceTimersByTime(60_000));
    await waitFor(() => expect(api.callsTo("GET", ME)).toHaveLength(2));
    await act(async () => vi.advanceTimersByTime(180_000)); // once, not every minute
    expect(api.callsTo("GET", ME)).toHaveLength(2);
  });

  it("a server error is explained (with its reference), never the backend's text, and can be retried", async () => {
    let fail = true;
    const api = mockApi({
      [`GET ${ME}`]: () =>
        fail
          ? apiError(500, "server_error", "Traceback: KeyError 'owner_id' in dashboard/selectors.py")
          : { status: 200, body: makeDashboard() },
    });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The dashboard couldn't be loaded");
    expect(alert).toHaveTextContent("Something went wrong on our side");
    expect(alert).toHaveTextContent("Reference: req-test-1");
    expect(text()).not.toMatch(/Traceback|KeyError|selectors\.py/);
    expect(text()).not.toMatch(/₹/);

    fail = false;
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: /Total leads\s+1,234/ })).toBeInTheDocument();
    expect(api.callsTo("GET", ME)).toHaveLength(2);
  });

  it("a network failure says the server couldn't be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server");
  });

  it("403 says it isn't allowed (nothing to retry); 404 is the standard not-found page", async () => {
    mockApi({ [`GET ${ME}`]: apiError(403, "permission_denied", "You do not have permission.") });
    const first = renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(await screen.findByRole("alert")).toHaveTextContent("You can't view this dashboard");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    first.unmount();

    navigation.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    mockApi({ [`GET ${RAHUL_URL}`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<DashboardView />, { viewer: adminViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(text()).not.toMatch(/₹|Total leads/);
    // The not-found page alone, not under a "Dashboard" header (review: two h1s).
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["Page not found"]);
  });

  it("a refresh the server refuses takes the figures off the screen", async () => {
    let refuse = false;
    mockApi({
      [`GET ${ME}`]: () =>
        refuse ? apiError(403, "permission_denied", "You do not have permission.") : { status: 200, body: makeDashboard() },
    });
    const { client } = renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(await screen.findByText("₹15,00,000")).toBeInTheDocument();
    refuse = true;
    await act(() => client.refetchQueries({ queryKey: ["dashboard"] }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You can't view this dashboard");
    expect(text()).not.toMatch(/₹15,00,000|Asha Mehta|1,234/);
  });
});

describe("supporting lists", () => {
  it("organisation-wide, each new lead, meeting and task names whose it is", async () => {
    mockApi({
      [`GET ${ALL}`]: {
        status: 200,
        body: makeDashboard({ new_leads: [makeDashboardLead({ owner: PRIYA })] }),
      },
      "GET /api/v1/admin/users": { status: 200, body: { results: [], next: null, previous: null } },
    });
    renderWithProviders(<DashboardView />, { viewer: adminViewer });

    const leads = await screen.findByRole("region", { name: "New leads today" });
    const row = within(leads).getByRole("listitem");
    expect(row).toHaveTextContent("Asha Mehta");
    expect(row).toHaveTextContent("Assigned to Priya Patel");
    expect(within(row).getByRole("link", { name: "Asha Mehta" })).toHaveAttribute("href", expect.stringMatching(/^\/leads\//));
    expect(within(row).getByText("10:12 am")).toHaveAttribute("datetime", "2026-10-03T04:42:00Z");
    const tasks = screen.getByRole("region", { name: "Tasks requiring attention" });
    expect(within(tasks).getByRole("listitem")).toHaveTextContent("Rahul Sharma");
    expect(screen.getByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("Organization overview")).toBeInTheDocument();
  });

  it("in one person's workspace the rows don't repeat their name", async () => {
    mockApi({ [`GET ${ME}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    const leads = await screen.findByRole("region", { name: "New leads today" });
    expect(within(leads).queryByText(/Assigned to/)).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Upcoming meetings" })).queryByText("Rahul Sharma")).not.toBeInTheDocument();
  });

  it("flag overdue tasks in words and link each row to its page in this workspace", async () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    mockApi({ [`GET ${RAHUL_URL}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: adminViewer });

    const tasks = await screen.findByRole("region", { name: "Tasks requiring attention" });
    expect(within(tasks).getByText("Overdue")).toBeInTheDocument();
    expect(within(tasks).getByRole("link", { name: "Send the revised quotation" })).toHaveAttribute(
      "href",
      expect.stringMatching(new RegExp(`^/admin/users/${RAHUL_ID}/activities/`)),
    );
    const meetings = screen.getByRole("region", { name: "Upcoming meetings" });
    expect(within(meetings).getByRole("link", { name: "Asha Mehta" })).toHaveAttribute(
      "href",
      expect.stringMatching(new RegExp(`^/admin/users/${RAHUL_ID}/leads/`)),
    );
    expect(within(meetings).getByRole("link", { name: "View all 6 upcoming meetings" })).toBeInTheDocument();
  });

  it("every list link says where it goes (no two links share a name for different lists; review)", async () => {
    mockApi({
      [`GET ${ME}`]: {
        status: 200,
        body: makeDashboard({
          leads: { total: 1, new_today: 1 },
          activities: { open_tasks: 1, tasks_due_today: 0, overdue_tasks: 1, meetings_today: 1, upcoming_meetings: 1 },
        }),
      },
    });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    await screen.findByRole("region", { name: "Key figures" });
    const names = screen.getAllByRole("link").map((link) => link.textContent);
    for (const name of ["View today's new leads", "View upcoming meetings", "View open tasks"]) {
      expect(names.filter((n) => n === name)).toHaveLength(1);
    }
    expect(names).not.toContain("View in Activities");
  });
});

describe("cards and links stay in the workspace and open the list each figure counts", () => {
  function ListsOf({ segment }: { segment: string }) {
    const leads = useLeadListState(segment);
    const activities = useActivityListState(segment);
    return (
      <>
        <pre data-testid="leads">{JSON.stringify(leads.filters)}</pre>
        <pre data-testid="activities">{JSON.stringify(activities.applied)}</pre>
      </>
    );
  }
  const remembered = (segment: string) => {
    const probe = render(<ListsOf segment={segment} />);
    const result = {
      leads: JSON.parse(screen.getByTestId("leads").textContent!),
      activities: JSON.parse(screen.getByTestId("activities").textContent!),
    };
    probe.unmount();
    return result;
  };

  it("an admin viewing Rahul goes to Rahul's Leads, Pipeline and Activities", async () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    mockApi({ [`GET ${RAHUL_URL}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: adminViewer });

    const base = `/admin/users/${RAHUL_ID}`;
    const card = async (name: RegExp) => screen.findByRole("link", { name });
    expect(await card(/^Total leads/)).toHaveAttribute("href", `${base}/leads`);
    expect(await card(/^New leads today/)).toHaveAttribute("href", `${base}/leads`);
    expect(await card(/^Pipeline value/)).toHaveAttribute("href", `${base}/pipeline`);
    expect(await card(/^Weighted pipeline/)).toHaveAttribute("href", `${base}/pipeline`);
    expect(await card(/^Meetings/)).toHaveAttribute("href", `${base}/activities`);
    expect(await card(/^Tasks/)).toHaveAttribute("href", `${base}/activities`);

    fireEvent.click(await card(/^Tasks/));
    expect(remembered(RAHUL_ID).activities).toMatchObject({ tab: "task", status: "open", ordering: "scheduled" });
    expect(remembered("all").activities.tab).toBe("all"); // the organisation's list is untouched

    fireEvent.click(await card(/^Meetings/));
    expect(remembered(RAHUL_ID).activities).toMatchObject({
      tab: "meeting",
      status: "not_cancelled",
      dateFrom: "2026-10-03",
      dateTo: "2026-10-03",
    });

    fireEvent.click(await card(/^New leads today/));
    expect(remembered(RAHUL_ID).leads).toMatchObject({ createdFrom: "2026-10-03", createdTo: "2026-10-03", archived: false });
    fireEvent.click(await card(/^Total leads/));
    expect(remembered(RAHUL_ID).leads).toMatchObject({ createdFrom: "", createdTo: "", q: "", archived: false });

    fireEvent.click(screen.getByRole("link", { name: "View all 6 upcoming meetings" }));
    expect(remembered(RAHUL_ID).activities).toMatchObject({ tab: "meeting", status: "upcoming" });
  });

  it("the pipeline cards open the board unfiltered, so its totals are the card's (review)", async () => {
    function Board() {
      const board = useBoardState("me");
      return (
        <>
          <button type="button" onClick={() => board.setFilters({ closeFrom: "2026-12-01", closeTo: "2026-12-31" })}>
            narrow
          </button>
          <pre data-testid="board">{JSON.stringify(board.applied)}</pre>
        </>
      );
    }
    const probe = render(<Board />);
    fireEvent.click(screen.getByRole("button", { name: "narrow" }));
    probe.unmount();
    mockApi({ [`GET ${ME}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    fireEvent.click(await screen.findByRole("link", { name: /^Weighted pipeline/ }));
    render(<Board />);
    expect(JSON.parse(screen.getByTestId("board").textContent!)).toEqual(NO_BOARD_FILTERS);
  });

  it("a card opened in a new tab (modifier or middle click) leaves this tab's remembered list alone (review)", async () => {
    function Search() {
      const leads = useLeadListState("me");
      return (
        <button type="button" onClick={() => leads.setFilters({ q: "Apollo" })}>
          search
        </button>
      );
    }
    const probe = render(<Search />);
    fireEvent.click(screen.getByRole("button", { name: "search" }));
    probe.unmount();
    mockApi({ [`GET ${ME}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    const card = await screen.findByRole("link", { name: /^New leads today/ });
    fireEvent.click(card, { ctrlKey: true });
    fireEvent.click(card, { metaKey: true });
    fireEvent.click(card, { shiftKey: true });
    fireEvent.click(card, { button: 1 });
    expect(remembered("me").leads).toMatchObject({ q: "Apollo", createdFrom: "" });
    fireEvent.click(card); // a navigation in this tab does preset the list
    expect(remembered("me").leads).toMatchObject({ q: "", createdFrom: "2026-10-03" });
  });

  it("a salesperson's cards go to their own pages", async () => {
    mockApi({ [`GET ${ME}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(await screen.findByRole("link", { name: /^Total leads/ })).toHaveAttribute("href", "/leads");
    expect(screen.getByRole("link", { name: /^Tasks/ })).toHaveAttribute("href", "/activities");
    fireEvent.click(screen.getByRole("link", { name: /^Tasks/ }));
    expect(remembered("me").activities).toMatchObject({ tab: "task", status: "open" });
  });
});

describe("workspace isolation (no other user's figures, ever)", () => {
  it("Rahul → Priya: nothing of Rahul's while Priya's loads slowly or fails; Back reads Rahul's afresh", async () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    const priya = deferred<{ status: number; body?: unknown }>();
    const api = mockApi({
      [`GET ${RAHUL_URL}`]: { status: 200, body: RAHUL_DASHBOARD },
      [`GET ${PRIYA_URL}`]: () => priya.promise,
    });
    const { rerender } = renderWithProviders(<DashboardView />, { viewer: adminViewer });
    expect(await screen.findByText("₹1,11,111")).toBeInTheDocument();

    navigation.pathname = `/admin/users/${PRIYA_ID}/dashboard`;
    rerender(<DashboardView />);
    expect(await screen.findByRole("status")).toHaveTextContent("Loading the dashboard");
    for (const value of RAHUL_ONLY) expect(text()).not.toContain(value);

    await act(async () => priya.resolve(apiError(500, "server_error", "Boom.")));
    expect(await screen.findByRole("alert")).toHaveTextContent("The dashboard couldn't be loaded");
    for (const value of RAHUL_ONLY) expect(text()).not.toContain(value);

    navigation.pathname = `/admin/users/${RAHUL_ID}/dashboard`; // Back
    rerender(<DashboardView />);
    expect(await screen.findByText("₹1,11,111")).toBeInTheDocument();
    expect(api.callsTo("GET", RAHUL_URL)).toHaveLength(2); // read again, not served from a cache
  });

  it("A → B → A shows each user's own figures only", async () => {
    navigation.pathname = `/admin/users/${PRIYA_ID}/dashboard`;
    mockApi({
      [`GET ${RAHUL_URL}`]: { status: 200, body: RAHUL_DASHBOARD },
      [`GET ${PRIYA_URL}`]: { status: 200, body: PRIYA_DASHBOARD },
    });
    const { rerender } = renderWithProviders(<DashboardView />, { viewer: adminViewer });
    expect(await screen.findByText("₹7,77,777")).toBeInTheDocument();
    for (const value of RAHUL_ONLY) expect(text()).not.toContain(value);

    navigation.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    rerender(<DashboardView />);
    expect(await screen.findByText("₹1,11,111")).toBeInTheDocument();
    expect(text()).not.toMatch(/7,77,777|Priya's|77,777.70/);

    navigation.pathname = `/admin/users/${PRIYA_ID}/dashboard`;
    rerender(<DashboardView />);
    expect(await screen.findByText("₹7,77,777")).toBeInTheDocument();
    for (const value of RAHUL_ONLY) expect(text()).not.toContain(value);
  });

  it("a different person signing in on the same page never sees the previous one's figures", async () => {
    // Sign-out reloads the page and clears the cache; even without that, the dashboard
    // keeps nothing once it is left, so the next viewer's "me" starts from a skeleton.
    const client = createTestQueryClient();
    const second = deferred<{ status: number; body: Dashboard }>();
    let call = 0;
    mockApi({ [`GET ${ME}`]: () => (++call === 1 ? { status: 200, body: RAHUL_DASHBOARD } : second.promise) });
    const first = renderWithProviders(<DashboardView />, { viewer: makeViewer({ id: RAHUL_ID }), client });
    expect(await screen.findByText("₹1,11,111")).toBeInTheDocument();
    first.unmount();
    await new Promise((resolve) => setTimeout(resolve, 0)); // the cache entry is dropped

    renderWithProviders(<DashboardView />, { viewer: makeViewer({ id: PRIYA_ID }), client });
    expect(await screen.findByRole("status")).toHaveTextContent("Loading the dashboard");
    for (const value of RAHUL_ONLY) expect(text()).not.toContain(value);
    await act(async () => second.resolve({ status: 200, body: PRIYA_DASHBOARD }));
    expect(await screen.findByText("₹7,77,777")).toBeInTheDocument();
  });

  it("every request goes to the workspace in the URL, and the cache key carries it", async () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    const api = mockApi({ [`GET ${RAHUL_URL}`]: { status: 200, body: RAHUL_DASHBOARD } });
    const { client } = renderWithProviders(<WorkspaceDashboard workspace={{ kind: "user", userId: RAHUL_ID }} />, {
      viewer: adminViewer,
    });
    await screen.findByText("₹1,11,111");
    expect(api.calls.map((c) => c.path)).toEqual([RAHUL_URL]);
    expect(client.getQueryCache().getAll().map((q) => q.queryKey)).toEqual([["dashboard", RAHUL_ID]]);
  });
});

describe("accessibility", () => {
  it("figures are keyboard-reachable links in a labelled list; nothing relies on colour alone", async () => {
    mockApi({ [`GET ${ME}`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    const figures = await screen.findByRole("region", { name: "Key figures" });
    const cards = within(figures).getAllByRole("link");
    expect(cards).toHaveLength(6);
    for (const card of cards) {
      expect(card.tagName).toBe("A");
      expect(card).toHaveAttribute("href");
    }
    cards[0]!.focus();
    expect(cards[0]).toHaveFocus();
    // "Overdue" is written out on the task and in the Tasks figure, not just coloured.
    expect(within(figures).getByRole("link", { name: /1 overdue/ })).toBeInTheDocument();
    expect(screen.getByText("Overdue")).toBeInTheDocument();
  });
});
