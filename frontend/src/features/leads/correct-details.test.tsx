/**
 * A customer's request to correct their details (docs/privacy.md#correction), from their
 * lead's page: only the fields changed are sent, with the lead's version; a conflict is
 * explained and starts again from the latest details; nothing is offered for an erased
 * customer. And, for administrators who handle privacy requests, exporting the customer's
 * data: a request reference and a confirmed identity check, then the Data requests page.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeadView } from "@/features/workspace/views";
import type { Lead } from "@/lib/api/types";
import { adminViewer, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { makeCard, PIPELINES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/leads/x" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const LEAD_ID = "4d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a";
const RAHUL = { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true };
const LEAD_URL = `/api/v1/workspaces/me/leads/${LEAD_ID}`;
const CORRECTION = `${LEAD_URL}/correction`;

function makeLead(overrides: Partial<Lead> = {}): Lead {
  return {
    id: LEAD_ID,
    display_name: "Asha Mehta",
    first_name: "Asha",
    last_name: "Mehta",
    organization_name: "ABC Diagnostics",
    job_title: "",
    email: "asha@abc.example",
    phone: "9876543210",
    mobile: "",
    status: { key: "new", name: "New", category: "open" },
    source: null,
    rating: null,
    owner: RAHUL,
    last_contacted_at: null,
    archived_at: null,
    created_at: "2026-10-05T08:45:00Z",
    updated_at: "2026-10-05T08:45:00Z",
    version: 1,
    alternate_phone: "",
    address_line_1: "Plot 4",
    address_line_2: "",
    city: "Pune",
    state: "",
    postal_code: "",
    country: "IN",
    description: "",
    created_by: RAHUL,
    ...overrides,
  } as Lead;
}

function routes(workspace: string, overrides: Record<string, Parameters<typeof mockApi>[0][string]> = {}) {
  return mockApi({
    [`GET /api/v1/workspaces/${workspace}/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
    [`GET /api/v1/workspaces/${workspace}/opportunities`]: {
      status: 200,
      body: { results: [makeCard({ title: "Asha Mehta — Adams 8380 V-lite", stage_id: STAGES.new.id })], next: null, previous: null },
    },
    [`GET /api/v1/workspaces/${workspace}/pipelines`]: { status: 200, body: PIPELINES },
    ...overrides,
  });
}

async function openCorrection() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Correct details" }));
  return { user, dialog: screen.getByRole("dialog", { name: "Correct customer details" }) };
}

async function replace(user: ReturnType<typeof userEvent.setup>, field: HTMLElement, value: string) {
  await user.clear(field);
  if (value) await user.type(field, value);
}

beforeEach(() => {
  nav.pathname = `/leads/${LEAD_ID}`;
});

describe("correcting a customer's details", () => {
  it("sends only the changed fields with the lead's version, and says how many deals followed", async () => {
    const corrected = makeLead({ email: "asha.mehta@abc.example", city: "Mumbai", version: 2 });
    let current = makeLead();
    const api = routes("me", {
      [`GET ${LEAD_URL}`]: () => ({ status: 200, body: current }),
      [`POST ${CORRECTION}`]: () => {
        current = corrected;
        return { status: 200, body: { lead: corrected, corrected: ["city", "email"], opportunities: 2 } };
      },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const { user, dialog } = await openCorrection();
    // What it does, and what it never touches.
    expect(dialog).toHaveAccessibleDescription(
      "Corrects this customer's details, and every deal still showing the old value. Deal amounts, prices and history don't change.",
    );
    expect(within(dialog).getByRole("textbox", { name: "First name" })).toHaveValue("Asha");
    expect(within(dialog).getByRole("textbox", { name: "Address line 1" })).toHaveValue("Plot 4");
    await replace(user, within(dialog).getByRole("textbox", { name: "Email" }), "asha.mehta@abc.example");
    await replace(user, within(dialog).getByRole("textbox", { name: "City" }), " Mumbai ");
    const opportunityReads = api.callsTo("GET", "/api/v1/workspaces/me/opportunities").length;
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));

    expect(await screen.findByText("Customer details corrected; 2 deals updated.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.callsTo("POST", CORRECTION).map((call) => call.body)).toEqual([
      { version: 1, email: "asha.mehta@abc.example", city: "Mumbai" },
    ]);
    // The page shows the corrected customer, and the deals' copies are read again.
    const contact = screen.getByRole("region", { name: "Contact" });
    await waitFor(() => expect(within(contact).getByRole("link", { name: "asha.mehta@abc.example" })).toBeInTheDocument());
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/workspaces/me/opportunities").length).toBeGreaterThan(opportunityReads));
  });

  it("clearing a field sends it blank; changing nothing sends nothing", async () => {
    const api = routes("me", {
      [`POST ${CORRECTION}`]: { status: 200, body: { lead: makeLead({ phone: "", version: 2 }), corrected: ["phone"], opportunities: 0 } },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const { user, dialog } = await openCorrection();
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    expect(within(dialog).getByText(/Nothing has changed yet/)).toBeInTheDocument();
    expect(api.callsTo("POST", CORRECTION)).toHaveLength(0);
    await replace(user, within(dialog).getByRole("textbox", { name: "Phone" }), "");
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    expect(await screen.findByText("Customer details corrected.")).toBeInTheDocument();
    expect(api.callsTo("POST", CORRECTION)[0]!.body).toEqual({ version: 1, phone: "" });
  });

  it("a conflict is explained, and the correction starts again from the latest details", async () => {
    let latest = makeLead();
    const api = routes("me", {
      [`GET ${LEAD_URL}`]: () => ({ status: 200, body: latest }),
      [`POST ${CORRECTION}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 1
          ? apiError(409, "conflict", "This record was changed by someone else.")
          : { status: 200, body: { lead: makeLead({ ...latest, email: "new@abc.example", version: 4 }), corrected: ["email"], opportunities: 1 } },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const { user, dialog } = await openCorrection();
    await replace(user, within(dialog).getByRole("textbox", { name: "Email" }), "new@abc.example");
    latest = makeLead({ city: "Nagpur", version: 3 }); // someone else corrected the city meanwhile
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Someone else changed this customer's details meanwhile.");
    await user.click(within(alert).getByRole("button", { name: "Reload latest details" }));
    // The form now shows the latest details (their city kept), and nothing of theirs is undone.
    await waitFor(() => expect(within(dialog).getByRole("textbox", { name: "City" })).toHaveValue("Nagpur"));
    expect(within(dialog).getByRole("textbox", { name: "Email" })).toHaveValue("asha@abc.example");
    await replace(user, within(dialog).getByRole("textbox", { name: "Email" }), "new@abc.example");
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    expect(await screen.findByText("Customer details corrected; 1 deal updated.")).toBeInTheDocument();
    expect(api.callsTo("POST", CORRECTION).map((call) => call.body)).toEqual([
      { version: 1, email: "new@abc.example" },
      { version: 3, email: "new@abc.example" },
    ]);
  });

  it("shows the server's reason under its field, and other refusals in the dialog", async () => {
    let reply = apiError(400, "validation_error", "Invalid input.", { email: ["Enter a valid email address."] });
    routes("me", { [`POST ${CORRECTION}`]: () => reply });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const { user, dialog } = await openCorrection();
    const email = within(dialog).getByRole("textbox", { name: "Email" });
    await replace(user, email, "asha@");
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(email).toHaveAccessibleDescription("Enter a valid email address."));
    expect(email).toHaveFocus();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    reply = apiError(422, "business_rule_violation", "This customer's details were erased on request: there is nothing to correct.");
    await user.click(within(dialog).getByRole("button", { name: "Save correction" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("were erased on request");
  });

  it("isn't offered for an erased customer", async () => {
    routes("me", {
      [`GET ${LEAD_URL}`]: {
        status: 200,
        body: makeLead({ display_name: "[erased]", first_name: "[erased]", last_name: "", email: "", phone: "", organization_name: "" }),
      },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { level: 1, name: "[erased]" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Correct details" })).not.toBeInTheDocument();
  });
});

describe("exporting a customer's data", () => {
  const EXPORTS = "/api/v1/admin/privacy/exports";

  it("is for administrators who handle privacy requests only", async () => {
    routes("me");
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await screen.findByRole("button", { name: "Correct details" });
    expect(screen.queryByRole("button", { name: "Export data" })).not.toBeInTheDocument();
  });

  it("needs the request's reference and a confirmed identity check, then points to Data requests", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    const api = routes(RAHUL_ID, {
      [`POST ${EXPORTS}`]: (call: RecordedCall) => ({
        status: 202,
        body: { id: "e1", status: "queued", ...(call.body as object) },
      }),
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Export data" }));
    const dialog = screen.getByRole("dialog", { name: "Export Asha Mehta's data" });
    const reference = within(dialog).getByRole("textbox", { name: "Request reference" });
    const verified = within(dialog).getByRole("checkbox", { name: "I have verified the requester's identity" });
    expect(verified).not.toBeChecked();

    await user.click(within(dialog).getByRole("button", { name: "Request export" }));
    expect(reference).toHaveAccessibleDescription(expect.stringContaining("Enter the request's ticket or case reference."));
    expect(verified).toHaveAccessibleDescription("Confirm that you have verified the requester's identity.");
    expect(reference).toHaveFocus();

    // A reference, never a description of the person.
    await user.type(reference, "Asha asked by phone");
    await user.click(verified);
    await user.click(within(dialog).getByRole("button", { name: "Request export" }));
    expect(reference).toHaveAccessibleDescription(expect.stringContaining("Use the ticket or case reference only"));
    expect(api.callsTo("POST", EXPORTS)).toHaveLength(0);

    await user.clear(reference);
    await user.type(reference, "DSR-2026/014");
    await user.click(within(dialog).getByRole("button", { name: "Request export" }));
    expect(await within(dialog).findByText("Export requested")).toBeInTheDocument();
    expect(api.callsTo("POST", EXPORTS).map((call) => call.body)).toEqual([
      { subject_type: "lead", subject_id: LEAD_ID, reference: "DSR-2026/014", identity_verified: true },
    ]);
    expect(within(dialog).getByRole("link", { name: "Data requests" })).toHaveAttribute("href", "/admin/data-requests");
    expect(within(dialog).getByRole("button", { name: "Close" })).toHaveFocus();
  });

  it("explains a refusal (too many pending exports)", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    routes(RAHUL_ID, {
      [`POST ${EXPORTS}`]: apiError(429, "throttled", "You have too many exports being prepared. Try again in 5 minutes."),
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Export data" }));
    const dialog = screen.getByRole("dialog", { name: "Export Asha Mehta's data" });
    await user.type(within(dialog).getByRole("textbox", { name: "Request reference" }), "DSR-15");
    await user.click(within(dialog).getByRole("checkbox", { name: "I have verified the requester's identity" }));
    await user.click(within(dialog).getByRole("button", { name: "Request export" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("too many exports being prepared");
  });
});
