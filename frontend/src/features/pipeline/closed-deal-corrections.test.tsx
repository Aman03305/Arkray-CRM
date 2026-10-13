/**
 * A won or lost deal's value, probability and dates are its sales record (adversarial review of
 * 73ff1e3, M2): the server refuses a change through the normal edit (422 `deal_closed`). The
 * edit panel shows them read-only, says how they change (reopen the deal, or an administrator
 * corrects them), and never sends them. An administrator gets "Correct": the changed figures
 * and a required reason, at the version shown (a 409 reloads it), and the deal's History tab
 * lists every correction with its old and new values.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditOpportunityView, OpportunityView } from "@/features/workspace/views";
import { adminViewer, OPPORTUNITY_OPTIONS_ROUTE, salesViewer } from "@/test/fixtures";
import { makeOpportunity, OPPORTUNITY_ID, PIPELINE_ROUTES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { correctionRequest } from "./Corrections";
import { closedDraft, draftFromOpportunity } from "./draft";
import { forgetBoardState } from "./hooks";
import { closedFiguresNote } from "./OpportunityDrawer";

const nav = vi.hoisted(() => ({ pathname: "/pipeline/x", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const ME = `/api/v1/workspaces/me/opportunities/${OPPORTUNITY_ID}`;
const ALL = `/api/v1/workspaces/all/opportunities/${OPPORTUNITY_ID}`;
const EMPTY_PAGE = { status: 200, body: { results: [], next: null, previous: null } };
const CONFIG = {
  ...PIPELINE_ROUTES,
  ...OPPORTUNITY_OPTIONS_ROUTE,
  ...Object.fromEntries(
    [ME, ALL].flatMap((base) => [
      [`GET ${base}/history`, EMPTY_PAGE],
      [`GET ${base}/negotiated-prices`, EMPTY_PAGE],
      [`GET ${base}/corrections`, EMPTY_PAGE],
    ]),
  ),
};
const WON = makeOpportunity({
  status: "won",
  stage: STAGES.won,
  probability: "100.00",
  value: "1250000.00",
  closed_at: "2026-09-30T04:30:00Z",
  opportunity_date: "2026-09-20",
  expected_close_date: "2026-09-30",
});
const CORRECTION = {
  id: "c0ffee00-0000-4000-8000-000000000001",
  fields: ["value"],
  changes: [{ field: "value", before: "1250000.00", after: "1187500.00" }],
  status: "won",
  reason: "Credit note CN-118: 5% after installation.",
  actor: { id: "a1", full_name: "Anita Admin", is_active: true },
  occurred_at: "2026-10-02T06:00:00Z",
};

beforeEach(() => {
  nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  forgetBoardState();
});

describe("a closed deal's figures", () => {
  it("are read-only in the edit panel, which says how they change and never sends them", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}/edit`;
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME}`]: { status: 200, body: WON },
      [`PATCH ${ME}`]: (call: RecordedCall) => ({ status: 200, body: { ...WON, version: 3, ...(call.body as object) } }),
    });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "Edit opportunity" });
    for (const label of [/^Installation price/, /^Opportunity date/, /^Expected closing date/, /^Probability/]) {
      expect(within(drawer).queryByLabelText(label)).not.toBeInTheDocument();
    }
    expect(within(drawer).getByText(closedFiguresNote("won"))).toBeInTheDocument();
    expect(within(drawer).getByText("₹12,50,000")).toBeInTheDocument();
    // Its text stays editable: only that is sent.
    const description = within(drawer).getByLabelText(/^Description/);
    await user.clear(description);
    await user.type(description, "Installed in March.");
    await user.click(within(drawer).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.callsTo("PATCH", ME)).toHaveLength(1));
    expect(api.callsTo("PATCH", ME)[0]!.body).toEqual({ version: 2, description: "Installed in March." });
  });

  it("an open deal's stay editable", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}/edit`;
    mockApi({ ...CONFIG, [`GET ${ME}`]: { status: 200, body: makeOpportunity() } });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const drawer = await screen.findByRole("dialog", { name: "Edit opportunity" });
    expect(within(drawer).getByLabelText(/^Installation price/)).toBeInTheDocument();
    expect(within(drawer).getByLabelText(/^Opportunity date/)).toBeInTheDocument();
    expect(within(drawer).queryByText(closedFiguresNote("won"))).not.toBeInTheDocument();
  });

  it("are never part of an edit, whatever the form holds", () => {
    const base = draftFromOpportunity(WON);
    const edited = closedDraft(base, { ...base, value: "9999999", opportunity_date: "2025-01-01", description: "New text" });
    expect(edited.value).toBe(base.value);
    expect(edited.opportunity_date).toBe(base.opportunity_date);
    expect(edited.description).toBe("New text");
  });

  it("a salesperson is told to ask an administrator, and gets no Correct action", async () => {
    mockApi({ ...CONFIG, [`GET ${ME}`]: { status: 200, body: WON } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const deal = await screen.findByRole("region", { name: "Deal" });
    expect(within(deal).getByText(closedFiguresNote("won"))).toBeInTheDocument();
    expect(within(deal).queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
  });
});

describe("an administrator's correction", () => {
  it("sends the changed figures with the reason, at the version shown", async () => {
    const api = mockApi({
      ...CONFIG,
      [`GET ${ALL}`]: { status: 200, body: WON },
      [`POST ${ALL}/correct`]: { status: 200, body: { ...WON, value: "1187500.00", version: 3 } },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    const deal = await screen.findByRole("region", { name: "Deal" });
    expect(within(deal).getByText(closedFiguresNote("won", { canCorrect: true }))).toBeInTheDocument();
    await user.click(within(deal).getByRole("button", { name: "Correct" }));
    const dialog = screen.getByRole("dialog", { name: "Correct this deal" });
    const value = within(dialog).getByLabelText(/^Installation price/);
    await user.clear(value);
    await user.type(value, "1187500");
    await user.type(within(dialog).getByLabelText(/^Reason/), "Credit note CN-118");
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(api.callsTo("POST", `${ALL}/correct`)).toHaveLength(1));
    expect(api.callsTo("POST", `${ALL}/correct`)[0]!.body).toEqual({ version: 2, reason: "Credit note CN-118", value: "1187500" });
    expect(await screen.findByText(/Correction saved/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Correct this deal" })).not.toBeInTheDocument();
  });

  it("needs a reason and a changed figure before anything is sent", async () => {
    const api = mockApi({ ...CONFIG, [`GET ${ALL}`]: { status: 200, body: WON } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(within(await screen.findByRole("region", { name: "Deal" })).getByRole("button", { name: "Correct" }));
    const dialog = screen.getByRole("dialog", { name: "Correct this deal" });
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    expect(within(dialog).getByText("Say why this deal is corrected.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Reason/), "Nothing really");
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    expect(within(dialog).getByText("Change the installation price or a date to correct.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ALL}/correct`)).toHaveLength(0);
  });

  it("reloads the deal after a conflict and says so", async () => {
    const api = mockApi({
      ...CONFIG,
      [`GET ${ALL}`]: { status: 200, body: WON },
      [`POST ${ALL}/correct`]: apiError(409, "conflict", "The record was changed by someone else. Reload and try again."),
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(within(await screen.findByRole("region", { name: "Deal" })).getByRole("button", { name: "Correct" }));
    const dialog = screen.getByRole("dialog", { name: "Correct this deal" });
    await user.clear(within(dialog).getByLabelText(/^Opportunity date/));
    await user.type(within(dialog).getByLabelText(/^Opportunity date/), "2026-09-10");
    await user.type(within(dialog).getByLabelText(/^Reason/), "Typo");
    const before = api.callsTo("GET", ALL).length;
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    expect(await within(dialog).findByText(/It changed a moment ago/)).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("GET", ALL).length).toBeGreaterThan(before));
  });

  it("is not offered for an open deal, nor to an administrator without crm.manage_any", async () => {
    mockApi({ ...CONFIG, [`GET ${ALL}`]: { status: 200, body: makeOpportunity() } });
    const view = renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    expect(within(await screen.findByRole("region", { name: "Deal" })).queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
    view.unmount();
    mockApi({ ...CONFIG, [`GET ${ALL}`]: { status: 200, body: WON } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, {
      viewer: { ...adminViewer, capabilities: adminViewer.capabilities.filter((c) => c !== "crm.manage_any") },
    });
    expect(within(await screen.findByRole("region", { name: "Deal" })).queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
  });

  it("is listed on the History tab with its old and new figures and its reason", async () => {
    mockApi({
      ...CONFIG,
      [`GET ${ME}`]: { status: 200, body: { ...WON, value: "1187500.00" } },
      [`GET ${ME}/corrections`]: { status: 200, body: { results: [CORRECTION], next: null, previous: null } },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "History" }));
    const corrections = await screen.findByRole("region", { name: "Corrections" });
    expect(within(corrections).getByText(/Installation price: ₹12,50,000 →/)).toBeInTheDocument();
    expect(within(corrections).getByText("₹11,87,500")).toBeInTheDocument();
    expect(within(corrections).getByText("Reason: Credit note CN-118: 5% after installation.")).toBeInTheDocument();
    expect(within(corrections).getByText(/Anita Admin/)).toBeInTheDocument();
  });
});

describe("correctionRequest", () => {
  const deal = { value: "1250000.00", opportunity_date: "2026-09-20", expected_close_date: "2026-09-30", version: 4 };
  const form = { value: "1250000", opportunity_date: "2026-09-20", expected_close_date: "2026-09-30", reason: " Fix " };

  it("sends only what changed, a cleared close date as null", () => {
    expect(correctionRequest(deal, { ...form, expected_close_date: "" }).body).toEqual({ version: 4, reason: "Fix", expected_close_date: null });
  });

  it("refuses an unparseable amount or nothing to change", () => {
    expect(correctionRequest(deal, { ...form, value: "12,50,000.555" }).problems.value).toBeTruthy();
    expect(correctionRequest(deal, form).problems.non_field_errors).toEqual(["Change the installation price or a date to correct."]);
  });
});
