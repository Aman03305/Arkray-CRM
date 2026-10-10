/**
 * A closed deal whose customer was reassigned to someone else after it closed
 * (docs/authorization.md#historical-deals): the API leaves out the customer's name, contacts,
 * address and free-text custom values and says so (`customer_restricted`). The deal says it
 * once, plainly, instead of showing rows of empty fields, and its edit panel neither offers
 * nor sends what it can't show.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditOpportunityView, OpportunityView, PipelineView } from "@/features/workspace/views";
import type { PipelineDto } from "@/lib/api/types";
import { OPPORTUNITY_OPTIONS_ROUTE, salesViewer } from "@/test/fixtures";
import { makeBoard, makeCard, makeOpportunity, OPPORTUNITY_ID, PIPELINE, STAGES } from "@/test/pipeline-fixtures";
import { mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { forgetBoardState } from "./hooks";
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
  "GET /api/v1/workspaces/me/pipelines": { status: 200, body: { results: [WITH_FIELDS] } },
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

  it("its edit panel doesn't offer the hidden details, and saving sends only what changed", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}/edit`;
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME}`]: { status: 200, body: RESTRICTED },
      [`PATCH ${ME}`]: (call: RecordedCall) => ({ status: 200, body: { ...RESTRICTED, version: 3, work_load: (call.body as { work_load: string }).work_load } }),
    });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "Edit opportunity" });
    const customer = within(drawer).getByRole("region", { name: "Customer" });
    expect(customer).toHaveTextContent(RESTRICTED_CUSTOMER_NOTE);
    expect(within(customer).queryByRole("textbox")).not.toBeInTheDocument();
    // The hidden free-text field isn't offered (nor required); the number field is.
    expect(within(drawer).queryByLabelText(/Lab contact notes/)).not.toBeInTheDocument();
    expect(within(drawer).getByLabelText(/^Units/)).toBeInTheDocument();
    const workLoad = within(drawer).getByLabelText(/^Work load/);
    await user.clear(workLoad);
    await user.type(workLoad, "400 tests/day");
    await user.click(within(drawer).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.callsTo("PATCH", ME)).toHaveLength(1));
    expect(api.callsTo("PATCH", ME)[0]!.body).toEqual({ version: 2, work_load: "400 tests/day" });
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
      "GET /api/v1/workspaces/me/pipelines": { status: 200, body: { results: [PIPELINE] } },
      "GET /api/v1/workspaces/me/pipeline-board": { status: 200, body: makeBoard([card]) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    await screen.findAllByRole("link", { name: "Customer restricted — HbA1c analyser" });
    expect(screen.getAllByText("Customer details hidden").length).toBeGreaterThan(0);
    expect(screen.queryByText("Customer in another workspace")).not.toBeInTheDocument();
  });
});
