/**
 * Regression tests for the product enhancement phase's adversarial frontend review: one per
 * confirmed finding, each asserting the corrected behaviour.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConvertLeadDialog } from "@/features/pipeline/ConvertLeadDialog";
import { forgetBoardState } from "@/features/pipeline/hooks";
import { OpportunityView, PipelineView } from "@/features/workspace/views";
import type { PipelineDto, PipelineList } from "@/lib/api/types";
import { LEAD_ID, makeLead, salesViewer } from "@/test/fixtures";
import { makeBoard, makeOpportunity, OPPORTUNITY_ID, PIPELINE, PIPELINE_ROUTES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/pipeline", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const PIPELINES_URL = "/api/v1/workspaces/me/pipelines";
const BOARD = "/api/v1/workspaces/me/pipeline-board";
const MINE_ID = "0e0f5a61-2222-4a2b-8c3d-000000000002";
const MINE: PipelineDto = {
  ...PIPELINE,
  id: MINE_ID,
  key: "pmine",
  name: "Government Tender",
  owner: { id: "u1", full_name: "Priya Patel", is_active: true },
  is_default: false,
  version: 4,
  can_manage: true,
  custom_fields: [],
};
const LIST: PipelineList = { results: [PIPELINE, MINE] };

beforeEach(() => {
  nav.pathname = "/pipeline";
  nav.push.mockReset();
  nav.replace.mockReset();
  forgetBoardState();
});

async function openMySettings() {
  const user = userEvent.setup();
  await user.selectOptions(await screen.findByRole("combobox", { name: "Pipeline" }), MINE_ID);
  await user.click(await screen.findByRole("button", { name: "Pipeline settings" }));
  return { user, panel: screen.getByRole("dialog", { name: "Pipeline settings" }) };
}

describe("pipeline settings", () => {
  it("P1: Escape on the nested archive confirmation closes only it; the edits stay", async () => {
    mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: LIST },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([], { pipeline: { id: MINE_ID, key: "pmine", name: MINE.name } }) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const { user, panel } = await openMySettings();
    const name = within(panel).getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Edited name");
    await user.click(within(panel).getByRole("button", { name: "Archive pipeline" }));
    expect(screen.getByRole("alertdialog", { name: /Archive/ })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog", { name: /Archive/ })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Pipeline settings" })).toBeInTheDocument();
    expect(within(panel).getByLabelText("Name")).toHaveValue("Edited name");
  });

  it("P1: a new pipeline and its fields are one request; a field without choices is caught first", async () => {
    const created: PipelineDto = { ...MINE, id: "new-id", name: "Tenders" };
    const api = mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: { results: [PIPELINE] } },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) },
      [`POST ${PIPELINES_URL}`]: { status: 201, body: created },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New pipeline" }));
    const panel = screen.getByRole("dialog", { name: "New pipeline" });
    await user.type(within(panel).getByLabelText("Name"), "Tenders");
    await user.click(within(panel).getByRole("button", { name: "Add field" }));
    await user.type(within(panel).getByLabelText("Field 1 name"), "Segment");
    await user.selectOptions(within(panel).getByLabelText("Segment type"), "single_select");
    await user.click(within(panel).getByRole("button", { name: "Create pipeline" }));
    expect(await within(panel).findByText("Add at least one choice.")).toBeInTheDocument();
    expect(api.calls.some((c) => c.method !== "GET")).toBe(false);
    await user.type(within(panel).getByLabelText("Choices (one per line)"), "Hospital");
    await user.click(within(panel).getByRole("button", { name: "Create pipeline" }));
    await waitFor(() => expect(api.callsTo("POST", PIPELINES_URL)).toHaveLength(1));
    const body = api.callsTo("POST", PIPELINES_URL)[0]!.body as { custom_fields: unknown[] };
    expect(body.custom_fields).toEqual([{ name: "Segment", type: "single_select", required: false, options: [{ label: "Hospital" }] }]);
    expect(api.calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("P2: after a partial save, the retry continues from what was saved, at its version", async () => {
    let stagesCalls = 0;
    const api = mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: LIST },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([], { pipeline: { id: MINE_ID, key: "pmine", name: MINE.name } }) },
      [`PATCH ${PIPELINES_URL}/${MINE_ID}`]: { status: 200, body: { ...MINE, name: "Renamed", version: 5 } },
      [`PUT ${PIPELINES_URL}/${MINE_ID}/stages`]: (call: RecordedCall) =>
        ++stagesCalls === 1
          ? apiError(422, "business_rule_violation", "Stage “New” still has 2 opportunities: move them first.")
          : { status: 200, body: { ...MINE, name: "Renamed", version: (call.body as { version: number }).version + 1 } },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const { user, panel } = await openMySettings();
    const name = within(panel).getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Renamed");
    await user.click(within(panel).getByRole("button", { name: "Remove New" }));
    await user.click(within(panel).getByRole("button", { name: "Save" }));
    expect(await within(panel).findByText(/still has 2 opportunities/)).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(stagesCalls).toBe(2));
    expect(api.callsTo("PATCH", `${PIPELINES_URL}/${MINE_ID}`)).toHaveLength(1); // not renamed twice
    expect(api.callsTo("PUT", `${PIPELINES_URL}/${MINE_ID}/stages`).map((c) => (c.body as { version: number }).version)).toEqual([5, 5]);
  });

  it("P3: moving a stage to the top keeps the keyboard on that row", async () => {
    mockApi({ [`GET ${PIPELINES_URL}`]: { status: 200, body: { results: [PIPELINE] } }, [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New pipeline" }));
    const panel = screen.getByRole("dialog", { name: "New pipeline" });
    await user.click(within(panel).getByRole("button", { name: "Move Qualified up" }));
    await waitFor(() => expect(within(panel).getByRole("button", { name: "Move Qualified down" })).toHaveFocus());
  });
});

describe("converting a lead", () => {
  const CONVERT = `/api/v1/workspaces/me/leads/${LEAD_ID}/convert`;

  it("P1: into a negotiation stage asks for the price, and a pipeline's required field is asked too", async () => {
    const withField: PipelineDto = {
      ...PIPELINE,
      custom_fields: [{ id: "f-tender", name: "Tender number", type: "text", required: true, options: [], position: 0 }],
    };
    const api = mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: { results: [withField] } },
      [`POST ${CONVERT}`]: { status: 201, body: { lead: makeLead({ version: 4 }), opportunity: makeOpportunity() } },
    });
    renderWithProviders(<ConvertLeadDialog workspace={{ kind: "self" }} lead={makeLead()} onClose={() => undefined} onConverted={() => undefined} />, {
      viewer: salesViewer,
    });
    const user = userEvent.setup();
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(await within(dialog).findByLabelText("Stage"), STAGES.negotiation.id);
    await user.type(within(dialog).getByLabelText("Value (₹)"), "12,50,000");
    await user.click(within(dialog).getByRole("button", { name: "Convert lead" }));
    expect(await within(dialog).findByText("Enter the negotiated price.")).toBeInTheDocument();
    expect(within(dialog).getByText("Enter Tender number.")).toBeInTheDocument();
    expect(api.callsTo("POST", CONVERT)).toHaveLength(0);
    await user.type(within(dialog).getByLabelText("Negotiated price (₹)"), "11,00,000");
    await user.type(within(dialog).getByLabelText(/^Tender number/), "GEM/2026/7");
    await user.click(within(dialog).getByRole("button", { name: "Convert lead" }));
    await waitFor(() => expect(api.callsTo("POST", CONVERT)).toHaveLength(1));
    expect(api.callsTo("POST", CONVERT)[0]!.body).toMatchObject({
      stage: STAGES.negotiation.id,
      value: "1250000",
      negotiated_price: "1100000",
      custom_fields: { "f-tender": "GEM/2026/7" },
    });
  });
});

describe("the opportunity panel and page", () => {
  it("P2: a stray click beside the panel asks before throwing typing away", async () => {
    const api = mockApi({ ...PIPELINE_ROUTES, [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New opportunity" }));
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    await user.type(within(panel).getByLabelText("Opportunity name"), "Big deal");
    fireEvent.click(panel.parentElement!.querySelector('[aria-hidden="true"]')!);
    const confirm = screen.getByRole("alertdialog", { name: "Discard your changes?" });
    await user.click(within(confirm).getByRole("button", { name: "Keep editing" }));
    expect(within(panel).getByLabelText("Opportunity name")).toHaveValue("Big deal");
    await user.keyboard("{Escape}");
    await user.click(within(screen.getByRole("alertdialog", { name: "Discard your changes?" })).getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("dialog", { name: "New opportunity" })).not.toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("P2: Update price recovers from someone else's change: the retry sends the new version", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
    const ME = `/api/v1/workspaces/me/opportunities/${OPPORTUNITY_ID}`;
    let version = 2;
    const api = mockApi({
      ...PIPELINE_ROUTES,
      [`GET ${ME}`]: () => ({ status: 200, body: makeOpportunity({ stage: STAGES.negotiation, negotiated_price: "1100000.00", negotiated_at: "2026-09-29T06:00:00Z", version }) }),
      [`POST ${ME}/negotiated-prices`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 3
          ? { status: 201, body: makeOpportunity({ stage: STAGES.negotiation, negotiated_price: "1050000.00", version: 4 }) }
          : apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const deal = await screen.findByRole("region", { name: "Deal" });
    version = 3; // someone else changed it meanwhile
    await user.click(within(deal).getByRole("button", { name: "Update price" }));
    const dialog = screen.getByRole("dialog", { name: "Update negotiated price" });
    await user.type(within(dialog).getByLabelText("Negotiated price (₹)"), "10,50,000");
    await user.click(within(dialog).getByRole("button", { name: "Save price" }));
    expect(await within(dialog).findByText(/latest version is loaded/)).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("GET", ME).length).toBeGreaterThan(1));
    await user.click(within(dialog).getByRole("button", { name: "Save price" }));
    expect(await screen.findByText("Negotiated price recorded.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/negotiated-prices`).map((c) => (c.body as { version: number }).version)).toEqual([2, 3]);
  });
});
