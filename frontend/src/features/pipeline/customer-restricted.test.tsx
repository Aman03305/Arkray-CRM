/**
 * A closed deal whose customer was reassigned to someone else after it closed
 * (docs/authorization.md#historical-deals): the API leaves out the customer's name, contacts,
 * address, free-text custom values and the deal's other free text (review of 73ff1e3, L6) and
 * says so (`customer_restricted`). The deal says it once, plainly, instead of showing rows of
 * empty fields. Nothing of it may change for this viewer (what may describe the customer, and
 * a closed deal's figures: review of 73ff1e3, M2), so no edit panel is offered.
 */
import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditOpportunityView, OpportunityView, PipelineView } from "@/features/workspace/views";
import type { PipelineDto } from "@/lib/api/types";
import { OPPORTUNITY_OPTIONS_ROUTE, salesViewer } from "@/test/fixtures";
import { makeBoard, makeCard, makeOpportunity, OPPORTUNITY_ID, PIPELINE, pipelineRoutes, STAGES } from "@/test/pipeline-fixtures";
import { mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { forgetBoardState } from "./hooks";
import { closedFiguresNote, RESTRICTED_DETAILS_NOTE } from "./OpportunityDrawer";
import { RESTRICTED_CUSTOMER_NOTE } from "./PipelineBits";

const nav = vi.hoisted(() => ({ pathname: "/pipeline/x", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const ME = `/api/v1/workspaces/me/opportunities/${OPPORTUNITY_ID}`;
const WITH_FIELDS: PipelineDto = {
  ...PIPELINE,
  custom_fields: [
    { id: "f-notes", name: "Lab contact notes", type: "text", required: true, options: [], position: 0 },
    { id: "f-units", name: "Units", type: "number", required: false, options: [], position: 1 },
  ],
};
const CONFIG = {
  ...pipelineRoutes("/api/v1/workspaces/me/pipelines", { results: [WITH_FIELDS] }),
  [`GET ${ME}/history`]: { status: 200, body: { results: [], next: null, previous: null } },
  [`GET ${ME}/negotiated-prices`]: { status: 200, body: { results: [], next: null, previous: null } },
  ...OPPORTUNITY_OPTIONS_ROUTE,
};

/** As a restricted viewer receives it: no customer, the title from the organisation and instrument. */
const RESTRICTED = makeOpportunity({
  title: "Apollo Diagnostics — HbA1c analyser",
  status: "won",
  stage: STAGES.won,
  probability: "100.00",
  closed_at: "2026-09-30T04:30:00Z",
  lead: { id: null, restricted: true },
  customer_restricted: true,
  customer_name: "",
  contact_phone: "",
  contact_email: "",
  address: "",
  // The deal's free text is withheld from this viewer too (review of 73ff1e3, L6).
  description: "",
  work_load: "",
  expected_cpt: "",
  agreed_cpt: "",
  custom_fields: { "f-units": "4" },
});

beforeEach(() => {
  nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  forgetBoardState();
});

describe("a deal whose customer the viewer may no longer see", () => {
  it("says why its customer details are hidden, instead of showing them as missing", async () => {
    mockApi({ ...CONFIG, [`GET ${ME}`]: { status: 200, body: RESTRICTED } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { level: 1, name: "Apollo Diagnostics — HbA1c analyser" })).toBeInTheDocument();
    const customer = screen.getByRole("region", { name: "Customer" });
    expect(within(customer).getByText(RESTRICTED_CUSTOMER_NOTE)).toBeInTheDocument();
    expect(RESTRICTED_CUSTOMER_NOTE).toBe("Customer details hidden: this customer was reassigned to someone else after the deal closed.");
    // The organisation stays; no empty Customer, Phone, Email or Address rows.
    expect(within(customer).getByText("Account")).toBeInTheDocument();
    expect(within(customer).getByText("Apollo Diagnostics")).toBeInTheDocument();
    for (const row of ["Customer", "Phone", "Email", "Address"]) {
      expect(within(customer).queryByText(row, { selector: "dt" })).not.toBeInTheDocument();
    }
    expect(within(customer).queryByRole("link")).not.toBeInTheDocument();
    // A free-text custom value isn't sent: "Hidden", not "—"; other values stay.
    const more = await screen.findByRole("region", { name: "More details" });
    expect(within(more).getByText("Hidden")).toBeInTheDocument();
    expect(within(more).getByText("4")).toBeInTheDocument();
  });

  it("a deal whose customer is visible shows no such note", async () => {
    mockApi({ ...CONFIG, [`GET ${ME}`]: { status: 200, body: makeOpportunity() } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const customer = await screen.findByRole("region", { name: "Customer" });
    expect(within(customer).queryByText(RESTRICTED_CUSTOMER_NOTE)).not.toBeInTheDocument();
    expect(within(customer).getByRole("link", { name: "asha@apollo.example" })).toBeInTheDocument();
  });

  it("is read-only to them: no Edit, and the edit route opens no panel", async () => {
    // It offered the value, probability and dates (the rest read-only, review P3-2); a closed
    // deal's figures are frozen for everyone now (review of 73ff1e3, M2): nothing is left to
    // edit. The note says how they change.
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}/edit`;
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME}`]: { status: 200, body: RESTRICTED },
      [`PATCH ${ME}`]: (call: RecordedCall) => ({ status: 200, body: { ...RESTRICTED, version: 3, value: (call.body as { value: string }).value } }),
    });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { level: 1, name: "Apollo Diagnostics — HbA1c analyser" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Edit opportunity" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.getByText(closedFiguresNote("won"))).toBeInTheDocument();
    expect(screen.queryByText(RESTRICTED_DETAILS_NOTE)).not.toBeInTheDocument();
    expect(api.callsTo("PATCH", ME)).toHaveLength(0);
  });

  it("shows what may describe the customer as the API sends it: withheld, never as inputs", async () => {
    // Review P3-2 and review of 73ff1e3, L6: the work load, Expected CPT, lost reason,
    // description and free-text custom values come blank for this viewer (as Ask Arkray
    // withholds them); the instrument (one of a fixed list) and other values stay.
    const lostDeal = { ...RESTRICTED, status: "lost" as const, stage: STAGES.lost, probability: "0.00" };
    mockApi({ ...CONFIG, [`GET ${ME}`]: { status: 200, body: lostDeal } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const instrument = await screen.findByRole("region", { name: "Instrument" });
    expect(within(instrument).getByText("HbA1c analyser")).toBeInTheDocument();
    expect(within(instrument).queryByText("300 tests/day")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Description" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(await screen.findByRole("region", { name: "More details" })).getByText("4")).toBeInTheDocument();
  });

  it("a deal whose customer is visible offers them all for editing", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}/edit`;
    mockApi({ ...CONFIG, [`GET ${ME}`]: { status: 200, body: makeOpportunity({ custom_fields: { "f-units": "4" } }) } });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const drawer = await screen.findByRole("dialog", { name: "Edit opportunity" });
    // The pipeline's custom fields come with the pipeline alone (review round 1, DEALS-1).
    await within(drawer).findByLabelText(/^Units/);
    for (const label of [/^Work load/, /^Expected CPT/, /^Description/, /^Units/, /Lab contact notes/]) {
      expect(within(drawer).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(drawer).queryByText(RESTRICTED_DETAILS_NOTE)).not.toBeInTheDocument();
  });

  it("its card on the board names no one when there is no organisation", async () => {
    nav.pathname = "/pipeline";
    const card = makeCard({
      title: "Customer restricted — HbA1c analyser",
      account_name: "",
      status: "won",
      stage_id: STAGES.won.id,
      lead: { id: null, restricted: true },
      customer_restricted: true,
    });
    mockApi({
      ...pipelineRoutes("/api/v1/workspaces/me/pipelines", { results: [PIPELINE] }),
      "GET /api/v1/workspaces/me/pipeline-board": { status: 200, body: makeBoard([card]) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    await screen.findAllByRole("link", { name: "Customer restricted — HbA1c analyser" });
    expect(screen.getAllByText("Customer details hidden").length).toBeGreaterThan(0);
    expect(screen.queryByText("Customer in another workspace")).not.toBeInTheDocument();
  });
});
