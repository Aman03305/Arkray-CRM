/**
 * Phase 7 global search (docs/search.md#frontend): the shell's Search entry, the dialog's
 * keyboard and screen-reader behaviour, every state, workspace-preserving result links, and
 * the isolation guarantees: one workspace's results never on screen in another, not even
 * from a late answer, and nothing a record contains is ever interpreted as markup.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SearchResults } from "@/lib/api/types";
import { adminViewer, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { apiError, createTestQueryClient, mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

import { searchKeys } from "./api";
import { matchRanges } from "./Highlight";
import { normalizeQuery, queryState } from "./query";
import { SearchLauncher } from "./SearchLauncher";

const nav = vi.hoisted(() => ({ pathname: "/dashboard", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
}));

beforeEach(() => {
  nav.pathname = "/dashboard";
  nav.push.mockReset();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const ZWJ = String.fromCodePoint(0x200d);
const RLO = String.fromCodePoint(0x202e);
const LEAD = "11111111-1111-4111-8111-00000000000a";
const DEAL = "11111111-1111-4111-8111-00000000000b";
const TASK = "11111111-1111-4111-8111-00000000000c";
const MEETING = "11111111-1111-4111-8111-00000000000d";
const NOTE = "11111111-1111-4111-8111-00000000000e";
const person = (name: string) => ({ id: RAHUL_ID, full_name: name, is_active: true });
/** A record's customer (the API's `lead`): on a deal, task, meeting or note it is shown as the
 * customer's name, never as a lead; leads have their own group (ADR-0028). */
const leadRef = (name: string) => ({ id: LEAD, display_name: name, organization_name: "", restricted: false });
const RESTRICTED = { id: null, restricted: true } as const;
type Deal = SearchResults["opportunities"]["results"][number];

/** Results with one record of each kind, every text field carrying `mark`. */
function results(mark: string, overrides: Partial<SearchResults> = {}): SearchResults {
  return {
    query: mark,
    terms: [mark],
    leads: {
      results: [
        {
          id: LEAD,
          display_name: `${mark} Lead`,
          organization_name: `${mark} Org`,
          status: { key: "new", name: "New", category: "open" },
          owner: person("Rahul Sharma"),
        },
      ],
      has_more: false,
    },
    opportunities: {
      results: [
        {
          id: DEAL,
          title: `${mark} Deal`,
          status: "won",
          stage: { id: "s1", name: "Won" },
          account_name: `${mark} Account`,
          customer_name: `${mark} Customer`,
          lead: leadRef(`${mark} Clinic`),
          owner: person("Rahul Sharma"),
          customer_restricted: false,
        },
      ],
      has_more: false,
    },
    tasks: {
      results: [
        {
          id: TASK,
          title: `${mark} Task`,
          status: "open",
          priority: "normal",
          due_at: "2026-10-05T06:30:00Z",
          is_overdue: true,
          lead: leadRef(`${mark} Clinic`),
          owner: person("Rahul Sharma"),
        },
      ],
      has_more: false,
    },
    meetings: {
      results: [
        {
          id: MEETING,
          title: `${mark} Meeting`,
          status: "completed",
          starts_at: "2026-10-04T05:30:00Z",
          ends_at: "2026-10-04T06:30:00Z",
          location: `${mark} Mumbai`,
          is_overdue: false,
          lead: leadRef(`${mark} Clinic`),
          owner: person("Rahul Sharma"),
        },
      ],
      has_more: false,
    },
    notes: {
      results: [
        {
          id: NOTE,
          preview: `prefers ${mark} calls`,
          preview_truncated: true,
          preview_starts_mid_text: true,
          created_at: "2026-10-01T05:30:00Z",
          lead: leadRef(`${mark} Clinic`),
        },
      ],
      has_more: false,
    },
    ...overrides,
  };
}

const EMPTY: SearchResults = {
  query: "nothing",
  terms: ["nothing"],
  leads: { results: [], has_more: false },
  opportunities: { results: [], has_more: false },
  tasks: { results: [], has_more: false },
  meetings: { results: [], has_more: false },
  notes: { results: [], has_more: false },
};

function subjectRoute(id: string, name: string) {
  return { status: 200, body: { kind: "user", subject: { id, full_name: name, status: "active" } } };
}

function setup(viewer = salesViewer, client = createTestQueryClient()) {
  const user = userEvent.setup();
  const view = renderWithProviders(<SearchLauncher />, { viewer, client });
  return { user, ...view };
}

async function openSearch(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /search/i }));
  return screen.getByRole("combobox", { name: "Search leads, opportunities, tasks, meetings and notes" });
}

describe("the Search entry", () => {
  it("is a visible, labelled button in the shell that opens a search dialog", async () => {
    mockApi({});
    const { user } = setup();
    const button = screen.getByRole("button", { name: /search/i });
    expect(button).toHaveTextContent("Search leads, deals, activities");
    expect(button).toHaveAttribute("aria-haspopup", "dialog");
    expect(button).toHaveAttribute("aria-keyshortcuts", "Control+K Meta+K");
    const input = await openSearch(user);
    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
    expect(input).toHaveFocus();
    expect(screen.getByText("Searching your records.")).toBeInTheDocument();
  });

  it("closes with Escape and gives focus back to the button", async () => {
    mockApi({});
    const { user } = setup();
    await openSearch(user);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /search/i })).toHaveFocus();
  });

  it("opens with Ctrl+K and with Cmd+K", async () => {
    mockApi({});
    const { user } = setup();
    await user.keyboard("{Control>}k{/Control}");
    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.keyboard("{Meta>}k{/Meta}");
    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
  });

  it("leaves Ctrl+K alone while typing in a text area, during IME composition, or in another dialog", async () => {
    mockApi({});
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <textarea aria-label="Note" />
        <SearchLauncher />
      </>,
      { viewer: salesViewer },
    );
    await user.click(screen.getByRole("textbox", { name: "Note" }));
    await user.keyboard("{Control>}k{/Control}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    const composing = new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, isComposing: true });
    act(() => {
      document.body.dispatchEvent(composing);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    const other = document.createElement("div");
    other.setAttribute("aria-modal", "true");
    document.body.appendChild(other);
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    other.remove();
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
  });

  it("is disabled where the URL names no workspace", () => {
    nav.pathname = "/admin/users/not-a-user/pipeline";
    mockApi({});
    setup(adminViewer);
    expect(screen.getByRole("button", { name: /search/i })).toBeDisabled();
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("searching", () => {
  it("waits for a pause in typing, then sends one request for the whole query", async () => {
    const api = mockApi({ "GET /api/v1/workspaces/me/search": { status: 200, body: results("Rahul") } });
    const { user } = setup();
    const input = await openSearch(user);
    await user.type(input, "Rahul");
    await screen.findByRole("option", { name: /^Opportunity: Rahul Deal/ });
    const calls = api.callsTo("GET", "/api/v1/workspaces/me/search");
    expect(calls).toHaveLength(1);
    expect(calls[0]!.query.get("q")).toBe("Rahul");
  });

  it("sends nothing until a word has 3 characters, and explains why", async () => {
    const api = mockApi({ "GET /api/v1/workspaces/me/search": { status: 200, body: EMPTY } });
    const { user } = setup();
    const input = await openSearch(user);
    await user.type(input, "Om");
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(api.calls).toHaveLength(0);
    expect(screen.getByText(/Type a word of at least 3 letters or digits/)).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-expanded", "false");
  });

  it("refuses invisible characters without asking the server", async () => {
    const api = mockApi({});
    const { user } = setup();
    const input = await openSearch(user);
    fireEvent.change(input, { target: { value: `Rahul${RLO}` } });
    expect(screen.getByRole("status")).toHaveTextContent("Remove the invisible or control characters from your search.");
    expect(input).toHaveAttribute("aria-invalid", "true");
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(api.calls).toHaveLength(0);
  });

  it("groups results under written-out headings, every kind told apart in words", async () => {
    mockApi({ "GET /api/v1/workspaces/me/search": { status: 200, body: results("Rahul") } });
    const { user } = setup();
    await user.type(await openSearch(user), "Rahul");
    const listbox = await screen.findByRole("listbox", { name: "Search results" });
    const groups = within(listbox).getAllByRole("group");
    expect(groups.map((g) => g.getAttribute("aria-labelledby") && document.getElementById(g.getAttribute("aria-labelledby")!)!.textContent)).toEqual([
      "Leads",
      "Opportunities",
      "Tasks",
      "Meetings",
      "Notes",
    ]);
    expect(within(groups[0]!).getByRole("option")).toHaveAccessibleName("Lead: Rahul Lead, Rahul Org");
    expect(within(groups[1]!).getByRole("option")).toHaveAccessibleName("Opportunity: Rahul Deal, Rahul Account, Rahul Customer, Won");
    expect(within(groups[2]!).getByRole("option")).toHaveAccessibleName(
      "Task: Rahul Task, Open, Due 5 Oct 2026, Overdue, Rahul Clinic",
    );
    expect(within(groups[3]!).getByRole("option")).toHaveAccessibleName(
      /^Meeting: Rahul Meeting, 4 Oct 2026, 11:00 am IST, Rahul Mumbai, Completed, Rahul Clinic$/i,
    );
    expect(within(groups[4]!).getByRole("option")).toHaveAccessibleName("Note: …prefers Rahul calls…, Rahul Clinic, Added 1 Oct 2026");
    expect(within(groups[4]!).getByRole("option")).toHaveTextContent("…prefers Rahul calls…");
    expect(await screen.findByRole("status")).toHaveTextContent("5 results");
  });

  it("lists the leads first, each opening its lead page in this workspace (ADR-0028)", async () => {
    const body = results("Ghost");
    mockApi({
      "GET /api/v1/workspaces/me/search": {
        status: 200,
        body: { ...body, leads: { has_more: true, results: [...body.leads.results, { ...body.leads.results[0]!, id: DEAL }] } },
      },
    });
    const { user } = setup();
    await user.type(await openSearch(user), "Ghost");
    const listbox = await screen.findByRole("listbox", { name: "Search results" });
    await within(listbox).findByRole("option", { name: /^Opportunity: Ghost Deal/ });
    const groups = within(listbox).getAllByRole("group");
    const headings = groups.map((g) => document.getElementById(g.getAttribute("aria-labelledby")!)!.textContent);
    expect(headings.map((h) => h?.replace(/Top \d+ shown$/, ""))).toEqual(["Leads", "Opportunities", "Tasks", "Meetings", "Notes"]);
    expect(within(groups[0]!).getByText("Leads")).toBeInTheDocument();
    expect(within(groups[0]!).getByText("Top 2 shown")).toBeInTheDocument();
    const leads = within(groups[0]!).getAllByRole("option");
    expect(leads.map((o) => o.getAttribute("aria-label"))).toEqual(["Lead: Ghost Lead, Ghost Org", "Lead: Ghost Lead, Ghost Org"]);
    expect(leads.map((o) => o.getAttribute("href"))).toEqual([`/leads/${LEAD}`, `/leads/${DEAL}`]);
    expect(within(listbox).getAllByRole("option")).toHaveLength(6);
    // Only the leads open a lead page; the deal is still an opportunity.
    expect([...listbox.querySelectorAll('a[href*="/leads"]')]).toEqual(leads);
    expect(screen.getByRole("status")).toHaveTextContent(/^6 results, more match$/);
  });

  it("shows the Leads group alone when only leads matched", async () => {
    mockApi({
      "GET /api/v1/workspaces/me/search": {
        status: 200,
        body: { ...EMPTY, query: "Ghost", terms: ["Ghost"], leads: results("Ghost").leads },
      },
    });
    const { user } = setup();
    await user.type(await openSearch(user), "Ghost");
    const option = await screen.findByRole("option");
    expect(option).toHaveAccessibleName("Lead: Ghost Lead, Ghost Org");
    expect(option).toHaveAttribute("href", `/leads/${LEAD}`);
    expect(screen.queryByText("No matching CRM records", { selector: "p:not([role])" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/^1 result$/);
  });

  it("shows an opportunity's own account and customer names, once each, highlighted, else its customer's name", async () => {
    const body = results("x");
    const deal = body.opportunities.results[0]!;
    const deals: Deal[] = [
      // Both names, each with a matched word.
      { ...deal, id: "22222222-2222-4222-8222-000000000001", title: "Analyser upgrade", status: "open", stage: { id: "s2", name: "Proposal" }, account_name: "Apollo Diagnostics", customer_name: "Dr Mehta" },
      // The same name twice is shown once.
      { ...deal, id: "22222222-2222-4222-8222-000000000002", title: "Reagent contract", status: "open", stage: { id: "s2", name: "Proposal" }, account_name: "Apollo Labs", customer_name: "Apollo Labs" },
      // Only a customer name.
      { ...deal, id: "22222222-2222-4222-8222-000000000003", title: "Service plan", status: "open", stage: { id: "s2", name: "Proposal" }, account_name: "", customer_name: "Mehta Clinic" },
      // Neither: the customer record's name, highlighted too.
      { ...deal, id: "22222222-2222-4222-8222-000000000004", title: "Old deal", status: "open", stage: { id: "s2", name: "Proposal" }, account_name: "", customer_name: "", lead: leadRef("Apollo Hospital") },
      // Neither, and the customer record is in another workspace.
      { ...deal, id: "22222222-2222-4222-8222-000000000005", title: "Hidden deal", status: "open", stage: { id: "s2", name: "Proposal" }, account_name: "", customer_name: "", lead: RESTRICTED },
      // A name already in the deal's name (the server names it after its customer) isn't repeated.
      { ...deal, id: "22222222-2222-4222-8222-000000000006", title: "Dr Mehta — Adams 8180 V", status: "open", stage: { id: "s2", name: "Proposal" }, account_name: "Apollo Clinic", customer_name: "Dr Mehta" },
      { ...deal, id: "22222222-2222-4222-8222-000000000007", title: "Apollo Labs — Adams 8180 T", status: "open", stage: { id: "s2", name: "Proposal" }, account_name: "Apollo Labs", customer_name: "" },
    ];
    mockApi({
      "GET /api/v1/workspaces/me/search": {
        status: 200,
        body: { ...EMPTY, query: "apollo mehta", terms: ["apollo", "mehta"], opportunities: { has_more: false, results: deals } },
      },
    });
    const { user } = setup();
    await user.type(await openSearch(user), "apollo mehta");
    const options = await screen.findAllByRole("option");
    expect(options.map((o) => o.getAttribute("aria-label"))).toEqual([
      "Opportunity: Analyser upgrade, Apollo Diagnostics, Dr Mehta, Proposal, Open",
      "Opportunity: Reagent contract, Apollo Labs, Proposal, Open",
      "Opportunity: Service plan, Mehta Clinic, Proposal, Open",
      "Opportunity: Old deal, Apollo Hospital, Proposal, Open",
      "Opportunity: Hidden deal, Customer in another workspace, Proposal, Open",
      "Opportunity: Dr Mehta — Adams 8180 V, Apollo Clinic, Proposal, Open",
      "Opportunity: Apollo Labs — Adams 8180 T, Proposal, Open",
    ]);
    const marks = (option: HTMLElement) => Array.from(option.querySelectorAll("mark")).map((m) => m.textContent);
    expect(marks(options[0]!)).toEqual(["Apollo", "Mehta"]);
    expect(options[1]!.textContent!.match(/Apollo Labs/g)).toHaveLength(1);
    expect(marks(options[1]!)).toEqual(["Apollo"]);
    expect(marks(options[2]!)).toEqual(["Mehta"]);
    expect(marks(options[3]!)).toEqual(["Apollo"]);
    expect(options[3]).toHaveTextContent("Apollo Hospital · Proposal · Open");
    expect(options[4]).toHaveTextContent("Customer in another workspace");
    expect(marks(options[4]!)).toEqual([]);
    expect(options[5]!.textContent!.match(/Dr Mehta/g)).toHaveLength(1);
    expect(marks(options[5]!)).toEqual(["Mehta", "Apollo"]);
    expect(options[6]!.textContent!.match(/Apollo Labs/g)).toHaveLength(1);
    expect(options[6]).toHaveTextContent(/Adams 8180 T\s*Proposal · Open$/);
    for (const option of options) expect(option).not.toHaveTextContent(/lead/i);
  });

  it("names a customer it may not show as being in another workspace, for tasks, meetings and notes", async () => {
    const body = results("Kept");
    mockApi({
      "GET /api/v1/workspaces/me/search": {
        status: 200,
        body: {
          ...body,
          tasks: { has_more: false, results: [{ ...body.tasks.results[0]!, lead: RESTRICTED }] },
          meetings: { has_more: false, results: [{ ...body.meetings.results[0]!, lead: RESTRICTED }] },
          notes: { has_more: false, results: [{ ...body.notes.results[0]!, lead: RESTRICTED }] },
        },
      },
    });
    const { user } = setup();
    await user.type(await openSearch(user), "Kept");
    expect(await screen.findByRole("option", { name: /^Task: Kept Task/ })).toHaveAccessibleName(
      "Task: Kept Task, Open, Due 5 Oct 2026, Overdue, Customer in another workspace",
    );
    expect(screen.getByRole("option", { name: /^Meeting: Kept Meeting/ })).toHaveTextContent("Customer in another workspace");
    expect(screen.getByRole("option", { name: /^Note: / })).toHaveAccessibleName(
      "Note: …prefers Kept calls…, Customer in another workspace, Added 1 Oct 2026",
    );
    // A deal with its own customer names shows them, whatever its customer record may be.
    expect(screen.getByRole("option", { name: /^Opportunity: Kept Deal/ })).toHaveAccessibleName("Opportunity: Kept Deal, Kept Account, Kept Customer, Won");
    expect(screen.queryByText(/lead in another workspace/i)).not.toBeInTheDocument();
  });

  it("says when more matched than are shown", async () => {
    const body = results("Bulk");
    mockApi({
      "GET /api/v1/workspaces/me/search": { status: 200, body: { ...body, opportunities: { ...body.opportunities, has_more: true } } },
    });
    const { user } = setup();
    await user.type(await openSearch(user), "Bulk");
    expect(await screen.findByText("Top 1 shown")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("5 results, more match");
  });
});

describe("states", () => {
  it("shows searching, then no matching CRM records, announced politely", async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    mockApi({
      "GET /api/v1/workspaces/me/search": async () => {
        await held;
        return { status: 200, body: EMPTY };
      },
    });
    const { user } = setup();
    await user.type(await openSearch(user), "nothing");
    expect(await screen.findByText("Searching…", { selector: "p:not([role])" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Searching…");
    release();
    expect(await screen.findByText("No matching CRM records", { selector: "p:not([role])" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("No matching CRM records");
    expect(screen.queryByText(/hidden|permission|not allowed/i)).not.toBeInTheDocument();
  });

  it("shows a failure with a way to try again, never stale results", async () => {
    let fail = true;
    mockApi({
      "GET /api/v1/workspaces/me/search": () =>
        fail ? apiError(500, "server_error", "An unexpected error occurred.") : { status: 200, body: results("Again") },
    });
    const { user } = setup();
    await user.type(await openSearch(user), "Again");
    expect(await screen.findByRole("alert")).toHaveTextContent("An unexpected error occurred.");
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("option", { name: /^Opportunity: Again Deal/ })).toBeInTheDocument();
  });

  it("shows the API's reason for a refused query (no retry offered)", async () => {
    mockApi({
      "GET /api/v1/workspaces/me/search": apiError(400, "validation_error", "Some fields are invalid.", {
        q: ["Use at least 3 characters in one of the search words."],
      }),
    });
    const { user } = setup();
    await user.type(await openSearch(user), "abc");
    expect(await screen.findByRole("alert")).toHaveTextContent("Use at least 3 characters in one of the search words.");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});

describe("keyboard", () => {
  it("moves through results with Up and Down and opens one with Enter, in this workspace", async () => {
    mockApi({ "GET /api/v1/workspaces/me/search": { status: 200, body: results("Rahul") } });
    const { user } = setup();
    const input = await openSearch(user);
    await user.type(input, "Rahul");
    const options = await screen.findAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    expect(input).toHaveAttribute("aria-activedescendant", options[0]!.id);
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(options[2]).toHaveAttribute("aria-selected", "true");
    expect(input).toHaveAttribute("aria-activedescendant", options[2]!.id);
    expect(input).toHaveFocus();
    await user.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}");
    expect(options).toHaveLength(5);
    expect(options[4]).toHaveAttribute("aria-selected", "true"); // wraps
    await user.keyboard("{Enter}");
    expect(nav.push).toHaveBeenCalledWith(`/activities/${NOTE}`);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("result links stay in the current workspace", () => {
  const hrefs = () => screen.getAllByRole("option").map((o) => o.getAttribute("href"));

  it("own workspace", async () => {
    mockApi({ "GET /api/v1/workspaces/me/search": { status: 200, body: results("Mine") } });
    const { user } = setup(salesViewer);
    await user.type(await openSearch(user), "Mine");
    await screen.findAllByRole("option");
    expect(hrefs()).toEqual([`/leads/${LEAD}`, `/pipeline/${DEAL}`, `/activities/${TASK}`, `/activities/${MEETING}`, `/activities/${NOTE}`]);
  });

  it("the organisation (an administrator's own pages), with owners named", async () => {
    const api = mockApi({ "GET /api/v1/workspaces/all/search": { status: 200, body: results("Org") } });
    const { user } = setup(adminViewer);
    await user.type(await openSearch(user), "Org");
    await screen.findAllByRole("option");
    expect(screen.getByText("Searching all users' records.")).toBeInTheDocument();
    expect(hrefs().slice(0, 2)).toEqual([`/leads/${LEAD}`, `/pipeline/${DEAL}`]);
    expect(screen.getAllByRole("option")[0]).toHaveAccessibleName("Lead: Org Lead, Org Org, Rahul Sharma");
    expect(screen.getAllByRole("option")[1]).toHaveAccessibleName("Opportunity: Org Deal, Org Account, Org Customer, Won, Rahul Sharma");
    expect(api.calls.every((c) => c.path === "/api/v1/workspaces/all/search")).toBe(true);
  });

  it("a selected user's workspace: every link and request names that user", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/activities`;
    const api = mockApi({
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: subjectRoute(RAHUL_ID, "Rahul Sharma"),
      [`GET /api/v1/workspaces/${RAHUL_ID}/search`]: { status: 200, body: results("Omega") },
    });
    const { user } = setup(adminViewer);
    await user.type(await openSearch(user), "Omega");
    await screen.findAllByRole("option");
    expect(await screen.findByText("Searching Rahul Sharma's records.")).toBeInTheDocument();
    const base = `/admin/users/${RAHUL_ID}`;
    expect(hrefs()).toEqual([
      `${base}/leads/${LEAD}`,
      `${base}/pipeline/${DEAL}`,
      `${base}/activities/${TASK}`,
      `${base}/activities/${MEETING}`,
      `${base}/activities/${NOTE}`,
    ]);
    expect(api.calls.filter((c) => c.path.endsWith("/search")).every((c) => c.path === `/api/v1/workspaces/${RAHUL_ID}/search`)).toBe(true);
    await user.keyboard("{Enter}");
    expect(nav.push).toHaveBeenCalledWith(`${base}/leads/${LEAD}`);
  });

  it("an upper-case spelling of the user id searches the same (canonical) workspace", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID.toUpperCase()}/pipeline`;
    const api = mockApi({
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: subjectRoute(RAHUL_ID, "Rahul Sharma"),
      [`GET /api/v1/workspaces/${RAHUL_ID}/search`]: { status: 200, body: results("Omega") },
    });
    const { user } = setup(adminViewer);
    await user.type(await openSearch(user), "Omega");
    await screen.findAllByRole("option");
    expect(api.calls.some((c) => c.path === `/api/v1/workspaces/${RAHUL_ID}/search`)).toBe(true);
    expect(hrefs().slice(0, 2)).toEqual([`/admin/users/${RAHUL_ID}/leads/${LEAD}`, `/admin/users/${RAHUL_ID}/pipeline/${DEAL}`]);
  });
});

/** Record every text node and link that reaches the DOM from now on. */
function watchScreen() {
  const seen: string[] = [];
  const take = (records: MutationRecord[]) => {
    for (const record of records) {
      for (const node of Array.from(record.addedNodes)) seen.push(node.textContent ?? "", node instanceof Element ? node.outerHTML : "");
      if (record.type === "characterData") seen.push(record.target.textContent ?? "");
      if (record.type === "attributes" && record.target instanceof Element) seen.push(record.target.getAttribute("href") ?? "");
    }
  };
  const observer = new MutationObserver(take);
  observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["href"] });
  return {
    saw(marker: string) {
      take(observer.takeRecords());
      observer.disconnect();
      return seen.some((text) => text.includes(marker));
    },
  };
}

describe("workspace isolation", () => {
  function world() {
    const pending: Record<string, () => void> = {};
    const api = mockApi({
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: subjectRoute(RAHUL_ID, "Rahul Sharma"),
      [`GET /api/v1/workspaces/${PRIYA_ID}`]: subjectRoute(PRIYA_ID, "Priya Patel"),
      [`GET /api/v1/workspaces/${RAHUL_ID}/search`]: (call: RecordedCall) =>
        new Promise((resolve) => {
          pending[`rahul:${call.query.get("q")}`] = () => resolve({ status: 200, body: results("RAHUL-OMEGA") });
        }),
      [`GET /api/v1/workspaces/${PRIYA_ID}/search`]: { status: 200, body: results("PRIYA-ZETA") },
    });
    return { api, pending };
  }

  it("a late answer for Rahul never appears under Priya, not for one frame", async () => {
    const { pending } = world();
    nav.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    const client = createTestQueryClient();
    const { user, rerender } = setup(adminViewer, client);
    await user.type(await openSearch(user), "OMEGA");
    await waitFor(() => expect(pending["rahul:OMEGA"]).toBeDefined());
    expect(screen.getByText("Searching…", { selector: "p:not([role])" })).toBeInTheDocument();

    // Back/Forward (or any navigation) to Priya's workspace while Rahul's search is running.
    const screenWatch = watchScreen();
    nav.pathname = `/admin/users/${PRIYA_ID}/dashboard`;
    rerender(<SearchLauncher />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); // closed, nothing carried over

    await act(async () => pending["rahul:OMEGA"]!());
    await user.type(await openSearch(user), "ZETA");
    expect(await screen.findByRole("option", { name: /^Opportunity: PRIYA-ZETA Deal/ })).toBeInTheDocument();
    expect(screen.getByText("Searching Priya Patel's records.")).toBeInTheDocument();
    expect(screenWatch.saw("RAHUL-OMEGA")).toBe(false);
    expect(screenWatch.saw(RAHUL_ID)).toBe(false);

    // Rahul's request was cancelled when his dialog went away (or, had it finished, its answer
    // would sit in Rahul's own entry): nothing of his under any key of Priya's.
    const rahuls = client.getQueryData<SearchResults>(searchKeys.results({ kind: "user", userId: RAHUL_ID }, "OMEGA"));
    expect(rahuls === undefined || rahuls.query === "RAHUL-OMEGA").toBe(true);
    for (const query of client.getQueryCache().findAll({ queryKey: ["search", PRIYA_ID] })) {
      expect(JSON.stringify(query.state.data ?? null)).not.toContain("RAHUL");
    }
  });

  it("switching workspaces clears the dialog: what was typed and found stays behind", async () => {
    world();
    nav.pathname = `/admin/users/${PRIYA_ID}/pipeline`;
    const { user, rerender } = setup(adminViewer);
    await user.type(await openSearch(user), "ZETA");
    await screen.findByRole("option", { name: /^Opportunity: PRIYA-ZETA Deal/ });
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    rerender(<SearchLauncher />);
    expect(screen.queryByText(/PRIYA-ZETA/)).not.toBeInTheDocument();
    const input = await openSearch(user);
    expect(input).toHaveValue("");
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("keys every search by workspace and query, and keeps nothing in browser storage", async () => {
    world();
    nav.pathname = `/admin/users/${PRIYA_ID}/pipeline`;
    const client = createTestQueryClient();
    const { user } = setup(adminViewer, client);
    await user.type(await openSearch(user), "ZETA");
    await screen.findByRole("option", { name: /^Opportunity: PRIYA-ZETA Deal/ });
    const keys = client.getQueryCache().findAll({ queryKey: searchKeys.all }).map((q) => q.queryKey);
    expect(keys).toContainEqual(["search", PRIYA_ID, "ZETA"]);
    expect(keys.every((key) => key[1] === PRIYA_ID)).toBe(true);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });
});

describe("nothing is interpreted", () => {
  const PAYLOADS = [
    "<script>window.__pwned = 1</script>",
    '<img src=x onerror="window.__pwned=1">',
    '"><svg/onload=window.__pwned=1>',
    "&lt;b&gt;bold&lt;/b&gt; &amp; &#x3C;i&#x3E;",
    `javascript:alert(1) ${RLO}evil${ZWJ}`,
  ];

  it.each(PAYLOADS)("renders %s as the characters it is", async (payload) => {
    const body = results("x");
    const hostile: SearchResults = {
      ...body,
      query: payload,
      terms: ["img", "script"],
      leads: { has_more: false, results: [{ ...body.leads.results[0]!, display_name: payload, organization_name: payload }] },
      opportunities: {
        has_more: false,
        results: [{ ...body.opportunities.results[0]!, title: payload, account_name: payload, customer_name: `${payload} customer` }],
      },
      tasks: { has_more: false, results: [{ ...body.tasks.results[0]!, title: payload }] },
      meetings: { has_more: false, results: [{ ...body.meetings.results[0]!, title: payload, location: payload }] },
      notes: { has_more: false, results: [{ ...body.notes.results[0]!, preview: payload }] },
    };
    mockApi({ "GET /api/v1/workspaces/me/search": { status: 200, body: hostile } });
    const { user } = setup();
    await user.type(await openSearch(user), "hostile");
    const options = await screen.findAllByRole("option");
    expect(options).toHaveLength(5);
    const listbox = screen.getByRole("listbox");
    expect(listbox.querySelector("script, img, svg[onload], iframe, object, embed")).toBeNull();
    expect(listbox.querySelector("[onerror], [onload]")).toBeNull();
    expect(listbox.textContent).toContain(payload);
    expect(options[0]!.textContent).toContain(payload);
    expect(options[1]!.textContent).toContain(`${payload} customer`);
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    for (const option of options) expect(option.getAttribute("href")).toMatch(/^\/(leads|pipeline|activities)\/[0-9a-f-]{36}$/);
  });
});

describe("highlighting", () => {
  it("marks the searched words, case-insensitively, as text", async () => {
    const body = results("x");
    mockApi({
      "GET /api/v1/workspaces/me/search": {
        status: 200,
        body: {
          ...body,
          terms: ["rahul", "SHA"],
          opportunities: { has_more: false, results: [{ ...body.opportunities.results[0]!, account_name: "", customer_name: "Rahul Sharma" }] },
        },
      },
    });
    const { user } = setup();
    await user.type(await openSearch(user), "rahul sha");
    const option = await screen.findByRole("option", { name: /Rahul Sharma/ });
    expect(Array.from(option.querySelectorAll("mark")).map((m) => m.textContent)).toEqual(["Rahul", "Sha"]);
  });

  it("finds ranges safely", () => {
    expect(matchRanges("Rahul Sharma", ["rahul", "sha"])).toEqual([[0, 5], [6, 9]]);
    expect(matchRanges("aaaa", ["aa", "aaa"])).toEqual([[0, 4]]); // overlaps merged
    expect(matchRanges("Straße", ["stra"])).toEqual([]); // upper case changes length: no marks
    expect(matchRanges("x", [""])).toEqual([]);
  });
});

describe("query rules (mirroring the API)", () => {
  it("normalises like the API", () => {
    expect(normalizeQuery("  Rahul \t  Sharma ")).toBe("Rahul Sharma");
    expect(normalizeQuery(`Zoe${String.fromCodePoint(0x308)}`)).toBe("Zoë");
  });

  it.each([
    ["", "idle"],
    ["Om", "idle"],
    ["ab cd", "idle"],
    ["Om Prakash", "ready"],
    ["राम", "ready"],
    ["शर्मा", "ready"], // a virama inside the word
    [`on${String.fromCodePoint(0x308)}`, "idle"], // a combining accent NFC can't merge
    [`on${String.fromCodePoint(0x20dd)}`, "idle"], // an enclosing mark
    [`ab${String.fromCodePoint(0xfe0f)}`, "idle"], // a variation selector
    [`on${String.fromCodePoint(0x200d)}`, "idle"], // a joiner doesn't count
    [String.fromCodePoint(0x94d).repeat(3), "idle"], // marks without a letter
    [`ab${String.fromCodePoint(0x308)}c`, "idle"], // the accent breaks the run
    ["---", "idle"],
    ["a-b", "idle"],
    ["★★★ 😀😀😀", "idle"],
    [`${"x".repeat(96)} ab`, "ready"], // 99 characters
    [`क्${String.fromCodePoint(0x200c)}ष`, "ready"],
    [`Rahul${RLO}`, "invalid"],
    [`Rahul${String.fromCodePoint(0x200b)}`, "invalid"],
    ["x".repeat(101), "invalid"],
    [`${String.fromCodePoint(0x1f600).repeat(60)} abc`, "ready"], // 64 code points, 124 UTF-16 units
    [String.fromCodePoint(0x1f600).repeat(101), "invalid"],
  ])("%s is %s", (raw, kind) => {
    expect(queryState(raw).kind).toBe(kind);
  });
});
