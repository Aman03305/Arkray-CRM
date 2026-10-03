import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeadsView } from "@/features/workspace/views";
import { adminViewer, LEAD_OPTIONS, makeLeadListItem, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { forgetLeadListState } from "./list-state";

const nav = vi.hoisted(() => ({ pathname: "/leads", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const OPTIONS = { "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS } };
const page = (results: unknown[], next: string | null = null, previous: string | null = null) => ({
  status: 200,
  body: { results, next, previous },
});

beforeEach(() => {
  nav.pathname = "/leads";
  nav.push.mockReset();
  forgetLeadListState();
});

describe("a salesperson's Leads", () => {
  it("lists their own leads with status as text and links to each lead", async () => {
    const api = mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/me/leads": page([
        makeLeadListItem(),
        makeLeadListItem({ id: "11111111-1111-4111-8111-111111111111", display_name: "Metro Labs", first_name: "", last_name: "", organization_name: "Metro Labs", status: { key: "qualified", name: "Qualified", category: "qualified" } }),
      ]),
    });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const table = await screen.findByRole("table", { name: "Leads" });
    expect(within(table).getByRole("link", { name: "Asha Mehta" })).toHaveAttribute("href", `/leads/${makeLeadListItem().id}`);
    expect(within(table).getByText("Qualified")).toBeInTheDocument(); // not colour alone
    expect(within(table).queryByRole("columnheader", { name: "Owner" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New Lead" })).toHaveAttribute("href", "/leads/new");
    expect(api.callsTo("GET", "/api/v1/workspaces/me/leads")[0]!.query.get("page_size")).toBe("25");
  });

  it("offers a card list on phones with the same links", async () => {
    mockApi({ ...OPTIONS, "GET /api/v1/workspaces/me/leads": page([makeLeadListItem()]) });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const cards = await screen.findByRole("list", { name: "Leads" });
    expect(within(cards).getByRole("link", { name: "Asha Mehta" })).toHaveAttribute("href", `/leads/${makeLeadListItem().id}`);
  });

  it("shows a polished empty state without any sample data", async () => {
    mockApi({ ...OPTIONS, "GET /api/v1/workspaces/me/leads": page([]) });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "No leads yet" })).toBeInTheDocument();
    expect(screen.getByText("Add the people and prospects you're working with.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add a lead" })).toHaveAttribute("href", "/leads/new");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("searches after two characters, never sending a one-character query", async () => {
    const api = mockApi({ ...OPTIONS, "GET /api/v1/workspaces/me/leads": page([]) });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const search = await screen.findByRole("searchbox", { name: "Search leads" });
    await user.type(search, "a");
    expect(await screen.findByText("Type at least 2 characters to search.")).toBeInTheDocument();
    await user.type(search, "s");
    await waitFor(() =>
      expect(api.callsTo("GET", "/api/v1/workspaces/me/leads").some((c) => c.query.get("q") === "as")).toBe(true),
    );
    expect(api.callsTo("GET", "/api/v1/workspaces/me/leads").some((c) => c.query.get("q") === "a")).toBe(false);
  });

  it("filters, sorts and switches to archived leads through allowlisted parameters", async () => {
    const api = mockApi({ ...OPTIONS, "GET /api/v1/workspaces/me/leads": page([makeLeadListItem()]) });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await screen.findByRole("table", { name: "Leads" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "qualified");
    await user.selectOptions(screen.getByRole("combobox", { name: "Sort by" }), "name");
    await user.click(screen.getByRole("button", { name: "Archived" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/api/v1/workspaces/me/leads").at(-1)!;
      expect(Object.fromEntries(last.query)).toEqual({
        status: "qualified",
        ordering: "name",
        archived: "true",
        page_size: "25",
      });
    });
    expect(screen.getByRole("button", { name: "Archived" })).toHaveAttribute("aria-pressed", "true");
    // No owner filter outside the organisation-wide workspace.
    await user.click(screen.getByRole("button", { name: /More filters/ }));
    expect(screen.queryByRole("combobox", { name: "Owner" })).not.toBeInTheDocument();
  });

  it("pages with the cursor from the API's links", async () => {
    const api = mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/me/leads": (call: RecordedCall) =>
        call.query.get("cursor") === "page-2"
          ? page([makeLeadListItem({ id: "22222222-2222-4222-8222-222222222222", display_name: "Second Page" })], null, "http://x/api/v1/workspaces/me/leads?cursor=page-1b")
          : page([makeLeadListItem()], "http://backend:8000/api/v1/workspaces/me/leads?cursor=page-2&page_size=25"),
    });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await screen.findByRole("table", { name: "Leads" });
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findAllByText("Second Page")).not.toHaveLength(0);
    expect(api.callsTo("GET", "/api/v1/workspaces/me/leads").at(-1)!.query.get("cursor")).toBe("page-2");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("archives from the row menu with the version it showed, after confirmation", async () => {
    const lead = makeLeadListItem();
    const api = mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/me/leads": page([lead]),
      [`POST /api/v1/workspaces/me/leads/${lead.id}/archive`]: { status: 200, body: { ...lead, archived_at: "2026-09-30T00:00:00Z" } },
    });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Asha Mehta" }));
    await user.click(screen.getByRole("menuitem", { name: "Archive" }));
    const dialog = screen.getByRole("alertdialog", { name: "Archive Asha Mehta?" });
    expect(within(dialog).getByText(/Nothing is deleted/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Archive lead" }));
    expect(await screen.findByText("Asha Mehta was archived.")).toBeInTheDocument();
    expect(api.callsTo("POST", `/api/v1/workspaces/me/leads/${lead.id}/archive`)[0]!.body).toEqual({ version: 3 });
  });

  it("shows a useful error with a retry, never raw details", async () => {
    let calls = 0;
    mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/me/leads": () => {
        calls += 1;
        return calls === 1 ? apiError(500, "server_error", "Traceback: secret internals") : page([makeLeadListItem()]);
      },
    });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    expect(await screen.findByText("Leads couldn't be loaded")).toBeInTheDocument();
    expect(screen.queryByText(/Traceback/)).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table", { name: "Leads" })).toBeInTheDocument();
  });

  it("remembers filters when coming back to the list, in memory only", async () => {
    const api = mockApi({ ...OPTIONS, "GET /api/v1/workspaces/me/leads": page([makeLeadListItem()]) });
    const first = renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await screen.findByRole("option", { name: "Contacted" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "contacted");
    first.unmount();
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const status = await screen.findByRole("combobox", { name: "Filter by status" });
    await waitFor(() => expect(status).toHaveValue("contacted")); // once the options have loaded
    expect(api.callsTo("GET", "/api/v1/workspaces/me/leads").at(-1)!.query.get("status")).toBe("contacted");
    expect(window.location.search).toBe(""); // nothing in the URL or its history
    expect(window.sessionStorage.length + window.localStorage.length).toBe(0);
  });
});

describe("administrators", () => {
  it("see organisation-wide leads with owners, and can filter by owner", async () => {
    const api = mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/all/leads": page([makeLeadListItem()]),
      "GET /api/v1/assignees": page([{ id: RAHUL_ID, full_name: "Rahul Sharma", email: "rahul@example.test" }]),
    });
    renderWithProviders(<LeadsView />, { viewer: adminViewer });
    const table = await screen.findByRole("table", { name: "Leads" });
    expect(within(table).getByRole("columnheader", { name: "Owner" })).toBeInTheDocument();
    expect(within(table).getByText("Rahul Sharma")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /More filters/ }));
    const owner = await screen.findByRole("combobox", { name: "Owner" });
    await waitFor(() => expect(owner).toBeEnabled());
    await user.selectOptions(owner, RAHUL_ID);
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/workspaces/all/leads").at(-1)!.query.get("owner")).toBe(RAHUL_ID));
  });

  it("see only the selected user's leads in that user's workspace, with links that stay there", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads`;
    const api = mockApi({ ...OPTIONS, [`GET /api/v1/workspaces/${RAHUL_ID}/leads`]: page([makeLeadListItem()]) });
    renderWithProviders(<LeadsView />, { viewer: adminViewer });
    const table = await screen.findByRole("table", { name: "Leads" });
    expect(within(table).getByRole("link", { name: "Asha Mehta" })).toHaveAttribute(
      "href",
      `/admin/users/${RAHUL_ID}/leads/${makeLeadListItem().id}`,
    );
    expect(screen.getByRole("link", { name: "New Lead" })).toHaveAttribute("href", `/admin/users/${RAHUL_ID}/leads/new`);
    expect(api.calls.every((c) => !c.path.includes("/workspaces/all") && !c.path.includes("/workspaces/me"))).toBe(true);
  });

  it("never see one user's leads while another user's workspace is loading", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads`;
    let releasePriya: () => void = () => {};
    mockApi({
      ...OPTIONS,
      [`GET /api/v1/workspaces/${RAHUL_ID}/leads`]: page([makeLeadListItem({ display_name: "Rahul's Secret Lead" })]),
      [`GET /api/v1/workspaces/${PRIYA_ID}/leads`]: () =>
        new Promise((resolve) => {
          releasePriya = () => resolve(page([makeLeadListItem({ id: "33333333-3333-4333-8333-333333333333", display_name: "Priya's Lead" })]));
        }),
    });
    const view = renderWithProviders(<LeadsView />, { viewer: adminViewer });
    expect(await screen.findAllByText("Rahul's Secret Lead")).not.toHaveLength(0);
    const user = userEvent.setup();
    await screen.findByRole("option", { name: "Contacted" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "contacted");

    nav.pathname = `/admin/users/${PRIYA_ID}/leads`;
    view.rerender(<LeadsView />);
    expect(screen.queryByText("Rahul's Secret Lead")).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Filter by status" })).toHaveValue(""); // no filter carried over
    releasePriya();
    expect(await screen.findAllByText("Priya's Lead")).not.toHaveLength(0);
    expect(screen.queryByText("Rahul's Secret Lead")).not.toBeInTheDocument();
  });

  it("see not-found when the API refuses the workspace", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads`;
    mockApi({ ...OPTIONS, [`GET /api/v1/workspaces/${RAHUL_ID}/leads`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<LeadsView />, { viewer: adminViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });
});

describe("before the viewer is known", () => {
  it("requests nothing (the workspace is not yet known)", () => {
    const api = mockApi({});
    renderWithProviders(<LeadsView />, { viewer: null });
    expect(screen.getByText("Loading")).toBeInTheDocument();
    expect(api.calls).toHaveLength(0);
  });
});
