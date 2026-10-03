/**
 * Regression tests for the Phase 3 adversarial frontend review and the live walkthrough:
 * one per confirmed finding (F1-F11), each asserting the corrected behaviour so none can
 * silently return. (The reviewer's repros asserted the defects.)
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditOpportunityView, OpportunityView, PipelineView } from "@/features/workspace/views";
import type { Board } from "@/lib/api/types";
import { parseAmountInput } from "@/lib/money";
import { LEAD_OPTIONS, salesViewer } from "@/test/fixtures";
import { makeBoard, makeCard, makeOpportunity, OPPORTUNITY_ID, OTHER_OPPORTUNITY_ID, PIPELINES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { forgetBoardState } from "./hooks";

const nav = vi.hoisted(() => ({ pathname: "/pipeline", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const CONFIG = {
  "GET /api/v1/config/pipelines": { status: 200, body: PIPELINES },
  "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS },
};
const ME_BOARD = "/api/v1/workspaces/me/pipeline-board";
const ME = `/api/v1/workspaces/me/opportunities/${OPPORTUNITY_ID}`;
const moveUrl = (id: string) => `/api/v1/workspaces/me/opportunities/${id}/move`;

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
const columnOf = (name: string) => screen.getByRole("listitem", { name: new RegExp(`^${name}`) });
function dragTo(card: HTMLElement, target: HTMLElement) {
  const transfer = dataTransfer();
  fireEvent.dragStart(card, { dataTransfer: transfer });
  fireEvent.dragOver(target, { dataTransfer: transfer });
  fireEvent.drop(target, { dataTransfer: transfer });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((r, j) => {
    resolve = r;
    reject = j;
  });
  return { promise, resolve, reject };
}
function narrow() {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
}

beforeEach(() => {
  nav.pathname = "/pipeline";
  nav.push.mockReset();
  forgetBoardState();
});

describe("F1 (P1): filters changed while a move is in flight", () => {
  it("the failed move is rolled back on the board it was made on; the filtered board is untouched", async () => {
    const response = deferred<{ status: number; body: unknown }>();
    let offline = false;
    mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: (call: RecordedCall) => {
        if (offline) throw new TypeError("Failed to fetch");
        return call.query.get("expected_close_to")
          ? { status: 200, body: makeBoard([makeCard({ id: OTHER_OPPORTUNITY_ID, title: "October deal", expected_close_date: "2026-10-10" })]) }
          : { status: 200, body: makeBoard() };
      },
      [`POST ${moveUrl(makeCard().id)}`]: () => response.promise,
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const card = (await screen.findByRole("link", { name: "Hospital Analyzer Project" })).closest("article")!;
    dragTo(card, columnOf("Negotiation"));
    await waitFor(() => expect(within(columnOf("Negotiation")).getByText("Hospital Analyzer Project")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Expected close to"), { target: { value: "2026-10-31" } });
    expect(await screen.findByText("October deal")).toBeInTheDocument();
    offline = true;
    await act(async () => response.reject(new TypeError("Failed to fetch")));
    expect(await screen.findByText(/It is back where it was/)).toBeInTheDocument();
    // The October board still shows October.
    expect(screen.getByText("October deal")).toBeInTheDocument();
    expect(screen.queryByText("Hospital Analyzer Project")).not.toBeInTheDocument();
    // And the unfiltered board shows the card back in Proposal, as the server has it.
    fireEvent.change(screen.getByLabelText("Expected close to"), { target: { value: "" } });
    await waitFor(() => expect(within(columnOf("Proposal")).getByText("Hospital Analyzer Project")).toBeInTheDocument());
    expect(within(columnOf("Negotiation")).queryByText("Hospital Analyzer Project")).not.toBeInTheDocument();
  });
});

describe("F2: the next move after a successful one", () => {
  it("sends the version the server returned, even before the board reloads", async () => {
    let boardCalls = 0;
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: () => (++boardCalls === 1 ? { status: 200, body: makeBoard() } : apiError(503, "unavailable", "Try later.")),
      [`POST ${moveUrl(makeCard().id)}`]: (call: RecordedCall) => {
        const version = (call.body as { version: number }).version;
        const stage = (call.body as { stage: string }).stage === STAGES.negotiation.id ? STAGES.negotiation : STAGES.qualified;
        return { status: 200, body: makeOpportunity({ stage, version: version + 1 }) };
      },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Move to Negotiation" }));
    await screen.findByText('"Hospital Analyzer Project" moved to Negotiation.');
    await user.click(screen.getByRole("button", { name: "Move Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Move to Qualified" }));
    expect(await screen.findByText('"Hospital Analyzer Project" moved to Qualified.')).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(makeCard().id)).map((c) => (c.body as { version: number }).version)).toEqual([2, 3]);
  });
});

describe("F3 and F4: phone stage lists", () => {
  it("F3: a stage list starts again from its first page when the filters change", async () => {
    narrow();
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() },
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
    await screen.findByText("Page one deal");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Page two deal");
    fireEvent.change(screen.getByLabelText("Expected close to"), { target: { value: "2026-10-31" } });
    await waitFor(() => {
      const last = api.callsTo("GET", "/api/v1/workspaces/me/opportunities").at(-1)!;
      expect(last.query.get("expected_close_to")).toBe("2026-10-31");
    });
    expect(api.callsTo("GET", "/api/v1/workspaces/me/opportunities").at(-1)!.query.has("cursor")).toBe(false);
  });

  it("F4: after a move from a stage list, focus stays on the page, on the message saying where it went", async () => {
    narrow();
    let moved = false;
    mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: () => ({ status: 200, body: makeBoard([moved ? makeCard({ stage_id: STAGES.qualified.id, version: 3 }) : makeCard()]) }),
      "GET /api/v1/workspaces/me/opportunities": (call: RecordedCall) => ({
        status: 200,
        body: {
          results: call.query.get("stage") === (moved ? STAGES.qualified.id : STAGES.proposal.id) ? [makeCard()] : [],
          next: null,
          previous: null,
        },
      }),
      [`POST ${moveUrl(makeCard().id)}`]: () => {
        moved = true;
        return { status: 200, body: makeOpportunity({ stage: STAGES.qualified, version: 3 }) };
      },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const trigger = await screen.findByRole("button", { name: "Move Hospital Analyzer Project" });
    trigger.focus();
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("menuitem", { name: "Move to Qualified" }));
    const notice = await screen.findByText('"Hospital Analyzer Project" moved to Qualified.');
    await waitFor(() => expect(document.activeElement).not.toBe(document.body));
    expect(document.activeElement?.contains(notice)).toBe(true);
    // The implicitly chosen tab (Proposal) stays chosen: it doesn't jump after the move.
    expect(screen.getByRole("tab", { selected: true })).toHaveTextContent("Proposal");
  });
});

describe("F5: a second move while one is saving", () => {
  it("is explained instead of silently dropped", async () => {
    const response = deferred<{ status: number; body: unknown }>();
    const other = makeCard({ id: OTHER_OPPORTUNITY_ID, title: "Second deal" });
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard([makeCard(), other]) },
      [`POST ${moveUrl(makeCard().id)}`]: () => response.promise,
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Move to Negotiation" }));
    await user.click(screen.getByRole("button", { name: "Move Second deal" }));
    await user.click(screen.getByRole("menuitem", { name: "Move to Qualified" }));
    expect(await screen.findByText("The previous move is still being saved. Try again in a moment.")).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(OTHER_OPPORTUNITY_ID))).toHaveLength(0);
    await act(async () => response.resolve({ status: 200, body: makeOpportunity({ stage: STAGES.negotiation, version: 3 }) }));
  });
});

describe("F9: retrying in the won/lost dialog after a 409", () => {
  it("uses the version the reloaded board shows", async () => {
    let boardCalls = 0;
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME_BOARD}`]: () => ({ status: 200, body: makeBoard([makeCard({ version: ++boardCalls === 1 ? 2 : 3 })]) }),
      [`POST ${moveUrl(makeCard().id)}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 3
          ? { status: 200, body: makeOpportunity({ stage: STAGES.won, status: "won", version: 4 }) }
          : apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Mark as won" }));
    const dialog = screen.getByRole("alertdialog", { name: "Mark as won?" });
    await user.click(within(dialog).getByRole("button", { name: "Mark as won" }));
    expect(await within(dialog).findByText(/please try again/)).toBeInTheDocument();
    await waitFor(() => expect(boardCalls).toBeGreaterThan(1));
    await user.click(within(dialog).getByRole("button", { name: "Mark as won" }));
    expect(await screen.findByText('"Hospital Analyzer Project" was marked as won.')).toBeInTheDocument();
    expect(api.callsTo("POST", moveUrl(makeCard().id)).map((c) => (c.body as { version: number }).version)).toEqual([2, 3]);
  });
});

describe("F6 (P1) and F7: editing conflicts", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}/edit`;
  });

  it("F6: 'Apply my changes' keeps someone else's manual probability", async () => {
    let version = 2;
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME}`]: () => ({
        status: 200,
        body:
          version === 2
            ? makeOpportunity({ version: 2 })
            : makeOpportunity({ version: 3, probability: "60.00", probability_overridden: true, weighted_value: "750000.00" }),
      }),
      [`PATCH ${ME}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 2 ? apiError(409, "conflict", "Changed.") : { status: 200, body: makeOpportunity({ version: 4 }) },
    });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const title = await screen.findByLabelText("Title");
    await user.clear(title);
    await user.type(title, "Hospital Analyzer Project (phase 2)");
    version = 3;
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(await screen.findByRole("button", { name: "Apply my changes to the latest version" }));
    expect(screen.getByRole("checkbox", { name: /Set the probability manually/ })).toBeChecked();
    expect(screen.getByLabelText("Probability (%)")).toHaveValue("60");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.callsTo("PATCH", ME)).toHaveLength(2));
    expect(api.callsTo("PATCH", ME)[1]!.body).toEqual({ version: 3, title: "Hospital Analyzer Project (phase 2)" });
  });

  it("F7: an opportunity archived meanwhile keeps the form and the typing on screen", async () => {
    let version = 2;
    mockApi({
      ...CONFIG,
      [`GET ${ME}`]: () => ({
        status: 200,
        body: version === 2 ? makeOpportunity({ version: 2 }) : makeOpportunity({ version: 3, archived_at: "2026-09-30T05:00:00Z" }),
      }),
      [`PATCH ${ME}`]: apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const description = await screen.findByLabelText(/^Description/);
    await user.clear(description);
    await user.type(description, "Long notes the user typed");
    version = 3;
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Someone archived this opportunity while you were editing")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Long notes the user typed")).toBeInTheDocument();
  });
});

describe("F8: focus after closing actions on the opportunity page", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  });

  it.each([
    ["Mark as won", "Mark as won", { status: "won", stage: STAGES.won, probability: "100.00", version: 3 }, "Marked as won."],
    ["Archive", "Archive opportunity", { archived_at: "2026-09-30T05:00:00Z", version: 3 }, "Opportunity archived."],
  ] as const)("%s: focus lands on the page heading, not the document body", async (open, confirm, after, notice) => {
    let done = false;
    mockApi({
      ...CONFIG,
      [`GET ${ME}`]: () => ({ status: 200, body: done ? makeOpportunity(after as never) : makeOpportunity() }),
      [`GET ${ME}/history`]: { status: 200, body: { results: [], next: null, previous: null } },
      [`POST ${ME}/move`]: () => {
        done = true;
        return { status: 200, body: makeOpportunity(after as never) };
      },
      [`POST ${ME}/archive`]: () => {
        done = true;
        return { status: 200, body: makeOpportunity(after as never) };
      },
    });
    renderWithProviders(
      <main>
        <OpportunityView opportunityId={OPPORTUNITY_ID} />
      </main>,
      { viewer: salesViewer },
    );
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: open });
    button.focus();
    await user.keyboard("{Enter}");
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: confirm }));
    expect(await screen.findByText(notice)).toBeInTheDocument();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 })));
  });
});

describe("F10: amounts with misplaced commas", () => {
  it.each(["12,50,00", "1,5", "12,5", "1,2,3,4", "1250,000"])("refuses %j instead of guessing", (typed) => {
    expect(parseAmountInput(typed).ok).toBe(false);
  });

  it.each([
    ["12,50,000", "1250000"],
    ["1,250,000", "1250000"],
    ["1,00,00,000.50", "10000000.50"],
    ["9,99,99,99,99,999.99", "999999999999.99"],
  ])("accepts %j", (typed, value) => {
    expect(parseAmountInput(typed)).toEqual({ ok: true, value });
  });
});

describe("F11: an inverted expected-close range", () => {
  it("is explained next to the dates and never sent; the board stays", async () => {
    const api = mockApi({ ...CONFIG, [`GET ${ME_BOARD}`]: { status: 200, body: makeBoard() } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    await screen.findByRole("link", { name: "Hospital Analyzer Project" });
    fireEvent.change(screen.getByLabelText("Expected close from"), { target: { value: "2026-12-01" } });
    fireEvent.change(screen.getByLabelText("Expected close to"), { target: { value: "2026-10-01" } });
    expect(await screen.findByText(/The end date must be on or after the start date/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Hospital Analyzer Project" })).toBeInTheDocument();
    expect(screen.queryByText("The pipeline couldn't be loaded")).not.toBeInTheDocument();
    const sent = api.callsTo("GET", ME_BOARD).map((c) => [c.query.get("expected_close_from"), c.query.get("expected_close_to")]);
    expect(sent).not.toContainEqual(["2026-12-01", "2026-10-01"]);
  });
});

describe("walkthrough: the board after a change made on the opportunity's own page", () => {
  it("a cached board gets the new version at once, so dragging right away doesn't conflict", async () => {
    const board: Board = makeBoard();
    let slow: ReturnType<typeof deferred<{ status: number; body: unknown }>> | null = null;
    const api = mockApi({
      ...CONFIG,
      // After the edit, the board's reload is slow: the drag happens on the cached board.
      [`GET ${ME_BOARD}`]: () => (slow ? slow.promise : { status: 200, body: board }),
      [`GET ${ME}`]: { status: 200, body: makeOpportunity() },
      [`GET ${ME}/history`]: { status: 200, body: { results: [], next: null, previous: null } },
      [`PATCH ${ME}`]: { status: 200, body: makeOpportunity({ version: 3, value: "1500000.00" }) },
      [`POST ${moveUrl(makeCard().id)}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 3
          ? { status: 200, body: makeOpportunity({ stage: STAGES.negotiation, version: 4 }) }
          : apiError(409, "conflict", "Changed."),
    });
    const view = renderWithProviders(<PipelineView />, { viewer: salesViewer });
    await screen.findByRole("link", { name: "Hospital Analyzer Project" });
    // Edit it on its page (same cache), while the board would still serve version 2.
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}/edit`;
    view.rerender(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />);
    const user = userEvent.setup();
    const value = await screen.findByLabelText("Value (₹)");
    await user.clear(value);
    await user.type(value, "15,00,000");
    slow = deferred();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalled());
    nav.pathname = "/pipeline";
    view.rerender(<PipelineView />);
    const card = (await screen.findByRole("link", { name: "Hospital Analyzer Project" })).closest("article")!;
    dragTo(card, columnOf("Negotiation"));
    expect(await screen.findByText('"Hospital Analyzer Project" moved to Negotiation.')).toBeInTheDocument();
    expect((api.callsTo("POST", moveUrl(makeCard().id))[0]!.body as { version: number }).version).toBe(3);
    const pending = slow as ReturnType<typeof deferred<{ status: number; body: unknown }>> | null;
    await act(async () =>
      pending?.resolve({ status: 200, body: makeBoard([makeCard({ stage_id: STAGES.negotiation.id, version: 4 })]) }),
    );
  });

  it("the stage history keeps its rows while it reloads after a move", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
    const row = {
      id: 1,
      from_stage_id: null,
      to_stage_id: STAGES.proposal.id,
      from_stage_name: "",
      to_stage_name: "Proposal",
      from_status: "",
      to_status: "open",
      value: "1250000.00",
      probability: "50.00",
      lost_reason: "",
      actor: { id: "u1", full_name: "Rahul Sharma", is_active: true },
      occurred_at: "2026-09-20T04:30:00Z",
    };
    const slowHistory = deferred<{ status: number; body: unknown }>();
    let historyCalls = 0;
    mockApi({
      ...CONFIG,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity() },
      [`GET ${ME}/history`]: () => (++historyCalls === 1 ? { status: 200, body: { results: [row], next: null, previous: null } } : slowHistory.promise),
      [`POST ${ME}/move`]: { status: 200, body: makeOpportunity({ stage: STAGES.won, status: "won", version: 3 }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const history = await screen.findByRole("region", { name: "Stage history" });
    await within(history).findByText("Created in");
    await user.click(screen.getByRole("button", { name: "Mark as won" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Mark as won" }));
    await screen.findByText("Marked as won.");
    expect(within(history).getByText("Created in")).toBeInTheDocument(); // still there, not a skeleton
    await act(async () => slowHistory.resolve({ status: 200, body: { results: [row], next: null, previous: null } }));
  });
});
