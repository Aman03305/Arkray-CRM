import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PipelineView } from "@/features/workspace/views";
import type { Board } from "@/lib/api/types";
import { adminViewer, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { column, makeBoard, makeCard, makeOpportunity, OTHER_OPPORTUNITY_ID, PIPELINE_ROUTES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { forgetBoardState } from "./hooks";

const nav = vi.hoisted(() => ({ pathname: "/pipeline", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const CONFIG = PIPELINE_ROUTES;
const ME_BOARD = "/api/v1/workspaces/me/pipeline-board";
const moveUrl = (id: string, workspace = "me") => `/api/v1/workspaces/${workspace}/opportunities/${id}/move`;

function dataTransfer() {
  const data = new Map<string, string>();
  return {
    types: [] as string[],
    dropEffect: "none",
    effectAllowed: "all",
    setData(type: string, value: string) {
      data.set(type, value);
      this.types = [...data.keys()];
    },
    getData: (type: string) => data.get(type) ?? "",
  };
}

function columnOf(name: string) {
  return screen.getByRole("listitem", { name: new RegExp(`^${name}`) });
}

async function dragTo(card: HTMLElement, target: HTMLElement) {
  const transfer = dataTransfer();
  fireEvent.dragStart(card, { dataTransfer: transfer });
  fireEvent.dragOver(target, { dataTransfer: transfer });
  fireEvent.drop(target, { dataTransfer: transfer });
}

/** A promise the test resolves by hand, to look at the board while a move is in flight. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

beforeEach(() => {
  nav.pathname = "/pipeline";
  nav.push.mockReset();
  forgetBoardState();
});

describe("the board", () => {
  it("shows every stage in order with counts, exact INR values and cards", async () => {
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: {
        status: 200,
        body: makeBoard([makeCard(), makeCard({ id: OTHER_OPPORTUNITY_ID, stage_id: STAGES.won.id, status: "won", title: "Won deal", probability: "100.00" })]),
      },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const stages = await screen.findByRole("list", { name: "Stages" });
    expect(Array.from(stages.children).map((c) => within(c as HTMLElement).getByRole("heading", { level: 2 }).textContent)).toEqual([
      "New",
      "Qualified",
      "Proposal",
      "Negotiation",
      "Won",
      "Lost",
    ]);
    const proposal = columnOf("Proposal");
    expect(within(proposal).getByRole("link", { name: "Hospital Analyzer Project" })).toHaveAttribute(
      "href",
      `/pipeline/${makeCard().id}`,
    );
    expect(within(proposal).getByText("Apollo Diagnostics")).toBeInTheDocument(); // the account
    expect(within(proposal).getAllByText("₹12,50,000").length).toBeGreaterThan(0);
    expect(within(proposal).getByText("50%")).toBeInTheDocument();
    expect(within(proposal).getByText("15 Dec 2026")).toBeInTheDocument();
    // Totals: exactly what the server computed, open opportunities only.
    const totals = screen.getByRole("region", { name: "Pipeline totals" });
    expect(within(totals).getByText("₹12,50,000")).toBeInTheDocument();
    expect(within(totals).getByText("₹6,25,000")).toBeInTheDocument();
    expect(within(totals).getByText("(Open opportunities' value)")).toBeInTheDocument(); // what the figure is, for screen readers
    // Won/Lost are written out, never colour alone.
    expect(within(columnOf("Won")).getByRole("heading", { name: "Won" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New opportunity" })).toBeInTheDocument(); // opens a side panel
    expect(api.callsTo("GET", ME_BOARD)[0]!.query.get("cards_per_stage")).toBe("20");
  });

  it("marks overdue open opportunities in words", async () => {
    mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard([makeCard({ expected_close_date: "2001-01-01" })]) } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    expect(await screen.findByText("(overdue)")).toBeInTheDocument();
  });

  it("shows restricted leads without their name", async () => {
    mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard([makeCard({ account_name: "", lead: { id: null, restricted: true } })]) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    expect(await screen.findByText("Lead in another workspace")).toBeInTheDocument();
    expect(screen.queryByText("Asha Mehta")).not.toBeInTheDocument();
  });

  it("an empty pipeline says so, without invented data", async () => {
    mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard([]) } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "No opportunities yet" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Stages" })).not.toBeInTheDocument();
  });

  it("a crowded stage offers its full, paginated list", async () => {
    const board = makeBoard([makeCard()]);
    const crowded: Board = {
      ...board,
      columns: board.columns.map((c, i) =>
        i === 2 ? column("proposal", [makeCard()], { count: 57, next: "http://x/api/v1/workspaces/me/opportunities?cursor=abc" }) : c,
      ),
    };
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: crowded },
      "GET /api/v1/workspaces/me/opportunities": (call: RecordedCall) => ({
        status: 200,
        body: {
          results: [makeCard({ title: call.query.get("cursor") ? "Page two deal" : "Page one deal" })],
          next: call.query.get("cursor") ? null : "http://x/api/v1/workspaces/me/opportunities?cursor=p2",
          previous: null,
        },
      }),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "View all 57 in Proposal" }));
    expect(await screen.findByText("Page one deal")).toBeInTheDocument();
    const first = api.callsTo("GET", "/api/v1/workspaces/me/opportunities")[0]!;
    expect(Object.fromEntries(first.query)).toMatchObject({ stage: STAGES.proposal.id, ordering: "expected_close", page_size: "25" });
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Page two deal")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back to the board" }));
    expect(await screen.findByRole("list", { name: "Stages" })).toBeInTheDocument();
  });

  it("404 looks like any missing page; errors can be retried", async () => {
    mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: apiError(404, "not_found", "Not found.") });
    const first = renderWithProviders(<PipelineView />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    first.unmount();
    let calls = 0;
    mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: () => (++calls === 1 ? apiError(400, "validation_error", "Bad filter.") : { status: 200, body: makeBoard() }),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "Hospital Analyzer Project" })).toBeInTheDocument();
  });
});

describe("moving opportunities", () => {
  it("drag and drop calls the move operation with the card's version, moving the card at once", async () => {
    const response = deferred<{ status: number; body: unknown }>();
    let board: Board = makeBoard();
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: () => ({ status: 200, body: board }),
      [`POST ${moveUrl(makeCard().id)}`]: () => response.promise,
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const card = (await screen.findByRole("link", { name: "Hospital Analyzer Project" })).closest("article")!;
    await dragTo(card, columnOf("Qualified"));
    // Optimistic: already in Qualified while the server works.
    await waitFor(() => expect(within(columnOf("Qualified")).getByText("Hospital Analyzer Project")).toBeInTheDocument());
    expect(within(columnOf("Proposal")).queryByText("Hospital Analyzer Project")).not.toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(makeCard().id))[0]!.body).toEqual({ stage: STAGES.qualified.id, version: 2 });
    board = makeBoard([makeCard({ stage_id: STAGES.qualified.id, probability: "25.00", version: 3 })]);
    await act(async () => response.resolve({ status: 200, body: makeOpportunity({ stage: STAGES.qualified, version: 3 }) }));
    expect(await screen.findByText('"Hospital Analyzer Project" moved to Qualified.')).toBeInTheDocument();
    expect(within(columnOf("Qualified")).getByText("25%")).toBeInTheDocument();
  });

  it.each([
    ["a conflict", apiError(409, "conflict", "Changed."), /was changed by someone else/],
    ["a vanished record", apiError(404, "not_found", "Not found."), /no longer in this workspace/],
    ["a server error", apiError(500, "server_error", "Boom."), /Couldn't move .* to Qualified/],
  ])("after %s the card goes back to where the server has it", async (_label, reply, message) => {
    const api = mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() }, [`POST ${moveUrl(makeCard().id)}`]: reply });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const card = (await screen.findByRole("link", { name: "Hospital Analyzer Project" })).closest("article")!;
    await dragTo(card, columnOf("Qualified"));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(within(columnOf("Proposal")).getByText("Hospital Analyzer Project")).toBeInTheDocument();
    expect(within(columnOf("Qualified")).queryByText("Hospital Analyzer Project")).not.toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("GET", ME_BOARD).length).toBeGreaterThan(1)); // reloaded
  });

  it("a lost connection puts the card back and says so", async () => {
    mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() } });
    const original = globalThis.fetch;
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const card = (await screen.findByRole("link", { name: "Hospital Analyzer Project" })).closest("article")!;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "POST") throw new TypeError("Failed to fetch");
      return original(input, init);
    }));
    await dragTo(card, columnOf("Qualified"));
    expect(await screen.findByText(/Could not reach the server.*It is back where it was/)).toBeInTheDocument();
    expect(within(columnOf("Proposal")).getByText("Hospital Analyzer Project")).toBeInTheDocument();
  });

  it("the keyboard alternative uses the same operation and keeps focus on the card", async () => {
    let board: Board = makeBoard();
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: () => ({ status: 200, body: board }),
      [`POST ${moveUrl(makeCard().id)}`]: () => {
        board = makeBoard([makeCard({ stage_id: STAGES.qualified.id, probability: "25.00", version: 3 })]);
        return { status: 200, body: makeOpportunity({ stage: STAGES.qualified, version: 3 }) };
      },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const menu = await screen.findByRole("button", { name: "Move Hospital Analyzer Project" });
    menu.focus();
    await user.keyboard("{Enter}");
    const items = screen.getAllByRole("menuitem").map((i) => i.textContent);
    expect(items).toEqual(["Move to New", "Move to Qualified", "Move to Negotiation", "Mark as won", "Mark as lost"]);
    await user.click(screen.getByRole("menuitem", { name: "Move to Qualified" }));
    expect(await screen.findByText('"Hospital Analyzer Project" moved to Qualified.')).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(makeCard().id))[0]!.body).toEqual({ stage: STAGES.qualified.id, version: 2 });
    await waitFor(() => expect(document.activeElement).toHaveAccessibleName("Move Hospital Analyzer Project"));
  });

  it("marking as won asks first", async () => {
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() },
      [`POST ${moveUrl(makeCard().id)}`]: { status: 200, body: makeOpportunity({ stage: STAGES.won, status: "won" }) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Mark as won" }));
    const dialog = screen.getByRole("alertdialog", { name: "Mark as won?" });
    expect(within(dialog).getByText(/closed as won at 100%/)).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(makeCard().id))).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Mark as won" }));
    expect(await screen.findByText('"Hospital Analyzer Project" was marked as won.')).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(makeCard().id))[0]!.body).toEqual({ stage: STAGES.won.id, version: 2 });
  });

  it("marking as lost takes an optional reason", async () => {
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() },
      [`POST ${moveUrl(makeCard().id)}`]: { status: 200, body: makeOpportunity({ stage: STAGES.lost, status: "lost" }) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const card = (await screen.findByRole("link", { name: "Hospital Analyzer Project" })).closest("article")!;
    await dragTo(card, columnOf("Lost"));
    const dialog = screen.getByRole("alertdialog", { name: "Mark as lost?" });
    await user.type(within(dialog).getByLabelText(/Why was it lost/), "  Chose a competitor ");
    await user.click(within(dialog).getByRole("button", { name: "Mark as lost" }));
    await screen.findByText('"Hospital Analyzer Project" was marked as lost.');
    expect(api.callsTo("POST", moveUrl(makeCard().id))[0]!.body).toEqual({
      stage: STAGES.lost.id,
      version: 2,
      lost_reason: "Chose a competitor",
    });
  });

  it("a closed opportunity is reopened into an open stage, never moved between closed ones", async () => {
    const won = makeCard({ stage_id: STAGES.won.id, status: "won", probability: "100.00", closed_at: "2026-09-29T10:00:00Z" });
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard([won]) },
      [`POST ${moveUrl(won.id)}`]: { status: 200, body: makeOpportunity({ stage: STAGES.negotiation }) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const card = (await screen.findByRole("link", { name: "Hospital Analyzer Project" })).closest("article")!;
    await dragTo(card, columnOf("Lost"));
    expect(await screen.findByText(/is closed\. Reopen it by moving it to an open stage first/)).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(won.id))).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Move Hospital Analyzer Project" }));
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual([
      "Reopen in New",
      "Reopen in Qualified",
      "Reopen in Proposal",
      "Reopen in Negotiation",
    ]);
    await user.click(screen.getByRole("menuitem", { name: "Reopen in Negotiation" }));
    const dialog = screen.getByRole("alertdialog", { name: "Reopen in Negotiation?" });
    // Reopening into a negotiation stage asks for the negotiated price too.
    await user.click(within(dialog).getByRole("button", { name: "Reopen" }));
    expect(within(dialog).getByText("Enter the negotiated price.")).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(won.id))).toHaveLength(0);
    await user.type(within(dialog).getByLabelText("Negotiated price (₹)"), "9,50,000");
    await user.click(within(dialog).getByRole("button", { name: "Reopen" }));
    await screen.findByText('"Hospital Analyzer Project" was reopened in Negotiation.');
    expect(api.callsTo("POST", moveUrl(won.id))[0]!.body).toEqual({ stage: STAGES.negotiation.id, version: 2, negotiated_price: "950000" });
  });

  it("a failure inside the confirmation keeps the dialog open with the reason", async () => {
    mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() },
      [`POST ${moveUrl(makeCard().id)}`]: apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Mark as won" }));
    const dialog = screen.getByRole("alertdialog", { name: "Mark as won?" });
    await user.click(within(dialog).getByRole("button", { name: "Mark as won" }));
    expect(await within(dialog).findByText(/was changed by someone else/)).toBeInTheDocument();
    expect(within(columnOf("Proposal")).getByText("Hospital Analyzer Project")).toBeInTheDocument();
  });

  it("people who can only view get no move controls", async () => {
    // An admin role without crm.manage_any doesn't exist yet; a viewer lacking it sees
    // a user's board read-only.
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    mockApi({ ...CONFIG, [`GET /api/v1/workspaces/${RAHUL_ID}/pipeline-board`]: { status: 200, body: makeBoard() } });
    const viewerOnly = { ...adminViewer, capabilities: adminViewer.capabilities.filter((c) => c !== "crm.manage_any") };
    renderWithProviders(<PipelineView />, { viewer: viewerOnly });
    await screen.findByRole("link", { name: "Hospital Analyzer Project" });
    expect(screen.queryByRole("button", { name: /^Move / })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "New opportunity" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Hospital Analyzer Project" }).closest("article")).toHaveAttribute("draggable", "false");
  });
});

describe("workspaces", () => {
  it("an administrator's organisation-wide board shows owners and can filter by one", async () => {
    nav.pathname = "/pipeline";
    const api = mockApi({
      ...CONFIG,
      "GET /api/v1/workspaces/all/pipeline-board": { status: 200, body: makeBoard() },
      "GET /api/v1/assignees": { status: 200, body: { results: [{ id: PRIYA_ID, full_name: "Priya Patel", email: "p@example.test" }], next: null, previous: null } },
    });
    renderWithProviders(<PipelineView />, { viewer: adminViewer });
    expect(await screen.findByText("Rahul Sharma")).toBeInTheDocument(); // owner on the card
    const user = userEvent.setup();
    const owner = screen.getByRole("combobox", { name: "Owner" });
    await waitFor(() => expect(owner).toBeEnabled());
    await user.selectOptions(owner, PRIYA_ID);
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/workspaces/all/pipeline-board").at(-1)!.query.get("owner")).toBe(PRIYA_ID));
  });

  it("switching from Rahul's pipeline to Priya's never shows one of Rahul's cards under Priya", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    const priyaBoard = deferred<{ status: number; body: unknown }>();
    mockApi({
      ...CONFIG,
      [`GET /api/v1/workspaces/${RAHUL_ID}/pipeline-board`]: { status: 200, body: makeBoard([makeCard({ title: "Rahul's secret deal" })]) },
      [`GET /api/v1/workspaces/${PRIYA_ID}/pipeline-board`]: () => priyaBoard.promise,
    });
    const view = renderWithProviders(<PipelineView />, { viewer: adminViewer });
    expect(await screen.findByText("Rahul's secret deal")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Pipeline totals" })).toBeInTheDocument();
    nav.pathname = `/admin/users/${PRIYA_ID}/pipeline`;
    view.rerender(<PipelineView />);
    // Immediately, and while Priya's board is still loading: nothing of Rahul's.
    expect(screen.queryByText("Rahul's secret deal")).not.toBeInTheDocument();
    expect(screen.queryByText("₹12,50,000")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Pipeline totals" })).not.toBeInTheDocument();
    await act(async () => priyaBoard.resolve({ status: 200, body: makeBoard([makeCard({ title: "Priya's deal", value: "100.00" })]) }));
    expect(await screen.findByText("Priya's deal")).toBeInTheDocument();
    expect(screen.queryByText("Rahul's secret deal")).not.toBeInTheDocument();
  });

  it("Back to Rahul after Priya shows Rahul's board, not Priya's", async () => {
    nav.pathname = `/admin/users/${PRIYA_ID}/pipeline`;
    const rahul = deferred<{ status: number; body: unknown }>();
    mockApi({
      ...CONFIG,
      [`GET /api/v1/workspaces/${PRIYA_ID}/pipeline-board`]: { status: 200, body: makeBoard([makeCard({ title: "Priya's deal" })]) },
      [`GET /api/v1/workspaces/${RAHUL_ID}/pipeline-board`]: () => rahul.promise,
    });
    const view = renderWithProviders(<PipelineView />, { viewer: adminViewer });
    await screen.findByText("Priya's deal");
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    view.rerender(<PipelineView />);
    expect(screen.queryByText("Priya's deal")).not.toBeInTheDocument();
    await act(async () => rahul.resolve({ status: 200, body: makeBoard([makeCard({ title: "Rahul's deal" })]) }));
    expect(await screen.findByText("Rahul's deal")).toBeInTheDocument();
  });
});

describe("phones", () => {
  function narrow() {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
  }

  it("show one stage at a time from tabs instead of six squeezed columns", async () => {
    narrow();
    const board = makeBoard([makeCard()]);
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: board },
      "GET /api/v1/workspaces/me/opportunities": (call: RecordedCall) => ({
        status: 200,
        body: {
          results: call.query.get("stage") === STAGES.proposal.id ? [makeCard()] : [],
          next: null,
          previous: null,
        },
      }),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const tabs = await screen.findByRole("tablist", { name: "Stages" });
    expect(screen.queryByRole("list", { name: "Stages" })).not.toBeInTheDocument();
    expect(api.callsTo("GET", ME_BOARD)[0]!.query.get("cards_per_stage")).toBe("0"); // summaries only
    // The first stage with opportunities is selected.
    expect(within(tabs).getByRole("tab", { selected: true })).toHaveTextContent("Proposal");
    expect(await screen.findByRole("link", { name: "Hospital Analyzer Project" })).toBeInTheDocument();
    const user = userEvent.setup();
    within(tabs).getByRole("tab", { selected: true }).focus();
    await user.keyboard("{ArrowRight}");
    expect(within(tabs).getByRole("tab", { selected: true })).toHaveTextContent("Negotiation");
    expect(await screen.findByText("No opportunities in Negotiation.")).toBeInTheDocument();
  });
});

describe("filters", () => {
  it("expected close dates filter the board (cards, counts and totals come from the server)", async () => {
    const api = mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    await screen.findByRole("link", { name: "Hospital Analyzer Project" });
    fireEvent.change(screen.getByLabelText("Expected close from"), { target: { value: "2026-10-01" } });
    fireEvent.change(screen.getByLabelText("Expected close to"), { target: { value: "2026-10-31" } });
    await waitFor(() => {
      const last = api.callsTo("GET", ME_BOARD).at(-1)!;
      expect([last.query.get("expected_close_from"), last.query.get("expected_close_to")]).toEqual(["2026-10-01", "2026-10-31"]);
    });
    expect(api.callsTo("GET", ME_BOARD).at(-1)!.query.has("owner")).toBe(false); // own workspace: never an owner filter
  });

  it("filtered with no results offers to clear", async () => {
    mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: (call: RecordedCall) => ({ status: 200, body: call.query.get("expected_close_from") ? makeBoard([]) : makeBoard() }) });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    await screen.findByRole("link", { name: "Hospital Analyzer Project" });
    fireEvent.change(screen.getByLabelText("Expected close from"), { target: { value: "2030-01-01" } });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("link", { name: "Hospital Analyzer Project" })).toBeInTheDocument();
  });

  it("phones open the filters from a button that counts the active ones", async () => {
    mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    await screen.findByRole("link", { name: "Hospital Analyzer Project" });
    const toggle = screen.getByRole("button", { name: "Filters" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", screen.getByRole("group", { name: "Filter the pipeline" }).id);
    const user = userEvent.setup();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.change(screen.getByLabelText("Expected close from"), { target: { value: "2026-10-01" } });
    expect(screen.getByRole("button", { name: "Filters (1)" })).toBeInTheDocument();
  });
});




describe("negotiation", () => {
  it("dropping a card on a negotiation stage asks for the price first and moves nothing until then", async () => {
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() },
      [`POST ${moveUrl(makeCard().id)}`]: {
        status: 200,
        body: makeOpportunity({ stage: STAGES.negotiation, version: 3, negotiated_price: "1050000.50", negotiated_at: "2026-10-04T10:00:00Z" }),
      },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const card = (await screen.findByRole("link", { name: "Hospital Analyzer Project" })).closest("article")!;
    await dragTo(card, columnOf("Negotiation"));
    const dialog = await screen.findByRole("alertdialog", { name: "Move to Negotiation?" });
    // Not moved yet: still in Proposal, nothing sent.
    expect(within(columnOf("Proposal")).getByText("Hospital Analyzer Project")).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(makeCard().id))).toHaveLength(0);
    const price = within(dialog).getByLabelText("Negotiated price (₹)");
    expect(price).toHaveFocus();
    await user.type(price, "10,50,000.50");
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    await screen.findByText('"Hospital Analyzer Project" moved to Negotiation.');
    // An exact decimal string, never a float.
    expect(api.callsTo("POST", moveUrl(makeCard().id))[0]!.body).toEqual({
      stage: STAGES.negotiation.id,
      version: 2,
      negotiated_price: "1050000.50",
    });
  });

  it("cancelling leaves the card where it was", async () => {
    const api = mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Move to Negotiation" }));
    const dialog = screen.getByRole("alertdialog", { name: "Move to Negotiation?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(within(columnOf("Proposal")).getByText("Hospital Analyzer Project")).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(makeCard().id))).toHaveLength(0);
  });

  it("an invalid price is explained and never sent", async () => {
    const api = mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Move to Negotiation" }));
    const dialog = screen.getByRole("alertdialog", { name: "Move to Negotiation?" });
    await user.type(within(dialog).getByLabelText("Negotiated price (₹)"), "1e6");
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    expect(within(dialog).getByLabelText("Negotiated price (₹)")).toHaveAttribute("aria-invalid", "true");
    expect(api.callsTo("POST", moveUrl(makeCard().id))).toHaveLength(0);
  });
});
