import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeadView } from "@/features/workspace/views";
import { adminViewer, LEAD_ID, LEAD_OPTIONS, makeLead, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/leads/x", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const OPTIONS = { "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS } };
const ME = `/api/v1/workspaces/me/leads/${LEAD_ID}`;

beforeEach(() => {
  nav.pathname = `/leads/${LEAD_ID}`;
  nav.push.mockReset();
});

describe("lead detail", () => {
  it("shows the lead's sections, with the activity timeline reserved but not faked", async () => {
    mockApi({
      ...OPTIONS,
      [`GET ${ME}`]: {
        status: 200,
        body: makeLead({ address_line_1: "7 Hospital Road", description: "Needs two analysers.", mobile: "+44 20 7946 0958" }),
      },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { level: 1, name: "Asha Mehta" })).toBeInTheDocument();
    for (const section of ["Contact", "Organization and role", "Address", "Sales information", "Description", "Record details", "Activity"]) {
      expect(screen.getByRole("region", { name: section })).toBeInTheDocument();
    }
    expect(screen.getByText("Activities will appear here.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "asha@apollo.example" })).toHaveAttribute("href", "mailto:asha@apollo.example");
    expect(screen.getByRole("link", { name: "+44 20 7946 0958" })).toHaveAttribute("href", "tel:+442079460958");
    expect(screen.getByText(/7 Hospital Road/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute("href", `/leads/${LEAD_ID}/edit`);
    // Sales users can't reassign.
    expect(screen.queryByRole("button", { name: "Reassign" })).not.toBeInTheDocument();
  });

  it("a lead that doesn't exist and one that isn't yours look exactly alike", async () => {
    mockApi({ ...OPTIONS, [`GET ${ME}`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.queryByText("Asha Mehta")).not.toBeInTheDocument();
  });

  it("changes status with the version it showed", async () => {
    const api = mockApi({
      ...OPTIONS,
      [`GET ${ME}`]: { status: 200, body: makeLead() },
      [`POST ${ME}/status`]: (call: RecordedCall) => ({
        status: 200,
        body: makeLead({ status: { key: (call.body as { status: string }).status, name: "Qualified", category: "qualified" }, version: 4 }),
      }),
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Change status" }));
    const dialog = screen.getByRole("dialog", { name: "Change status" });
    await user.click(within(dialog).getByRole("radio", { name: "Qualified" }));
    await user.click(within(dialog).getByRole("button", { name: "Save status" }));
    expect(await screen.findByText("Status changed to Qualified.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/status`)[0]!.body).toEqual({ status: "qualified", version: 3 });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("explains a conflicting status change and reloads the latest version", async () => {
    let version = 3;
    const api = mockApi({
      ...OPTIONS,
      [`GET ${ME}`]: () => ({ status: 200, body: makeLead({ version }) }),
      [`POST ${ME}/status`]: apiError(409, "conflict", "The record was changed by someone else."),
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Change status" }));
    version = 4; // someone else saved
    await user.click(screen.getByRole("radio", { name: "Contacted" }));
    await user.click(screen.getByRole("button", { name: "Save status" }));
    expect(await screen.findByText(/Someone else changed this lead a moment ago/)).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("GET", ME).length).toBeGreaterThan(1));
  });

  it("an archived lead says so, can be restored, and can't be edited", async () => {
    mockApi({ ...OPTIONS, [`GET ${ME}`]: { status: 200, body: makeLead({ archived_at: "2026-09-29T10:00:00Z" }) } });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByText("This lead is archived")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change status" })).not.toBeInTheDocument();
  });
});

describe("reassignment by an administrator", () => {
  const RAHUL_LEAD = `/api/v1/workspaces/${RAHUL_ID}/leads/${LEAD_ID}`;

  it("moving a lead out of the viewed user's workspace returns to that workspace's list", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    const api = mockApi({
      ...OPTIONS,
      [`GET ${RAHUL_LEAD}`]: { status: 200, body: makeLead() },
      "GET /api/v1/assignees": {
        status: 200,
        body: { results: [{ id: PRIYA_ID, full_name: "Priya Patel", email: "priya@example.test" }], next: null, previous: null },
      },
      [`POST ${RAHUL_LEAD}/assign`]: {
        status: 200,
        body: makeLead({ owner: { id: PRIYA_ID, full_name: "Priya Patel", is_active: true }, version: 4 }),
      },
    });
    const view = renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Reassign" }));
    const dialog = screen.getByRole("dialog", { name: "Reassign Asha Mehta" });
    const owner = within(dialog).getByRole("combobox", { name: "New owner" });
    await waitFor(() => expect(owner).toBeEnabled());
    await user.selectOptions(owner, PRIYA_ID);
    await user.click(within(dialog).getByRole("button", { name: "Reassign lead" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/admin/users/${RAHUL_ID}/leads`));
    expect(api.callsTo("POST", `${RAHUL_LEAD}/assign`)[0]!.body).toEqual({ owner: PRIYA_ID, version: 3 });
    // Live-walkthrough regression: the lead is not re-requested in Rahul's workspace,
    // where it no longer exists (that request was a 404).
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(api.callsTo("GET", RAHUL_LEAD)).toHaveLength(1);
    view.unmount();
    expect(view.client.getQueryData(["leads", "detail", RAHUL_ID, LEAD_ID])).toBeUndefined();
  });

  it("requires choosing someone", async () => {
    nav.pathname = `/leads/${LEAD_ID}`;
    const api = mockApi({
      ...OPTIONS,
      [`GET /api/v1/workspaces/all/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      "GET /api/v1/assignees": { status: 200, body: { results: [], next: null, previous: null } },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Reassign" }));
    await user.click(screen.getByRole("button", { name: "Reassign lead" }));
    expect(await screen.findByText("Choose who should own this lead.")).toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
  });
});
