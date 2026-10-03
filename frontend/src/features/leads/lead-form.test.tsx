import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditLeadView, NewLeadView } from "@/features/workspace/views";
import { adminViewer, LEAD_ID, LEAD_OPTIONS, makeLead, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/leads/new", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const OPTIONS = { "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS } };
const NO_DUPLICATES = { "GET /api/v1/workspaces/me/leads/duplicates": { status: 200, body: { results: [] } } };

beforeEach(() => {
  nav.pathname = "/leads/new";
  nav.push.mockReset();
});

async function ready() {
  await screen.findByRole("option", { name: "Referral" }); // options loaded
}

describe("creating a lead", () => {
  it("is organised in labelled sections, and a name alone is enough", async () => {
    const api = mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      "POST /api/v1/workspaces/me/leads": { status: 201, body: makeLead({ first_name: "Asha", last_name: "" }) },
    });
    renderWithProviders(<NewLeadView />, { viewer: salesViewer });
    await ready();
    for (const section of ["Basic information", "Contact information", "Sales information", "Address", "Additional information"]) {
      expect(screen.getByRole("region", { name: section })).toBeInTheDocument();
    }
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("First name"), "  Asha ");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/leads/${LEAD_ID}`));
    const [call] = api.callsTo("POST", "/api/v1/workspaces/me/leads");
    expect(call!.body).toEqual({ first_name: "Asha", status: "new" });
    expect(call!.headers["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("asks for a person or an organisation before sending anything", async () => {
    const api = mockApi({ ...OPTIONS, ...NO_DUPLICATES });
    renderWithProviders(<NewLeadView />, { viewer: salesViewer });
    await ready();
    await userEvent.setup().click(screen.getByRole("button", { name: "Create lead" }));
    const first = screen.getByLabelText("First name");
    expect(first).toHaveAttribute("aria-invalid", "true");
    expect(first).toHaveAccessibleDescription(/Enter the person's name or their organization/);
    expect(first).toHaveFocus();
    expect(api.callsTo("POST", "/api/v1/workspaces/me/leads")).toHaveLength(0);
  });

  it("shows the server's validation next to the field and focuses it", async () => {
    mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      "POST /api/v1/workspaces/me/leads": apiError(400, "validation_error", "Some fields are invalid.", {
        email: ["Enter a valid email address."],
      }),
    });
    renderWithProviders(<NewLeadView />, { viewer: salesViewer });
    await ready();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("First name"), "Asha");
    await user.type(screen.getByLabelText(/^Email/), "asha@");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    const email = await screen.findByLabelText(/^Email/);
    await waitFor(() => expect(email).toHaveAttribute("aria-invalid", "true"));
    expect(email).toHaveAccessibleDescription("Enter a valid email address.");
    expect(email).toHaveFocus();
  });

  it("can't be submitted twice while saving, and a retry reuses the same request key", async () => {
    let attempts = 0;
    let release: () => void = () => {};
    const api = mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      "POST /api/v1/workspaces/me/leads": () => {
        attempts += 1;
        if (attempts === 1) return apiError(503, "service_unavailable", "Try again.");
        return new Promise((resolve) => {
          release = () => resolve({ status: 201, body: makeLead() });
        });
      },
    });
    renderWithProviders(<NewLeadView />, { viewer: salesViewer });
    await ready();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("First name"), "Asha");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await screen.findByText(/temporarily unavailable|Something went wrong|Try again/);
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await user.click(screen.getByRole("button", { name: "Create lead" })); // double click while saving
    release();
    await waitFor(() => expect(nav.push).toHaveBeenCalled());
    const keys = api.callsTo("POST", "/api/v1/workspaces/me/leads").map((c) => c.headers["Idempotency-Key"]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]); // same content, same key: the server replays, never duplicates
  });

  it("a changed form is a new request with a new key", async () => {
    const api = mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      "POST /api/v1/workspaces/me/leads": apiError(400, "validation_error", "Some fields are invalid.", { phone: ["Bad."] }),
    });
    renderWithProviders(<NewLeadView />, { viewer: salesViewer });
    await ready();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("First name"), "Asha");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await screen.findByText("Bad.");
    await user.type(screen.getByLabelText("First name"), "!");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/me/leads")).toHaveLength(2));
    const [a, b] = api.callsTo("POST", "/api/v1/workspaces/me/leads").map((c) => c.headers["Idempotency-Key"]);
    expect(a).not.toBe(b);
  });

  it("points out possible duplicates without blocking", async () => {
    mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/me/leads/duplicates": (call: RecordedCall) => ({
        status: 200,
        body: {
          results:
            call.query.get("email") === "asha@apollo.example"
              ? [{ id: LEAD_ID, display_name: "Asha Mehta", organization_name: "Apollo", owner: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true }, archived_at: null, matched_on: ["email"] }]
              : [],
        },
      }),
    });
    renderWithProviders(<NewLeadView />, { viewer: salesViewer });
    await ready();
    await userEvent.setup().type(screen.getByLabelText(/^Email/), "asha@apollo.example");
    expect(await screen.findByText("A lead with these contact details already exists", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Asha Mehta" })).toHaveAttribute("href", `/leads/${LEAD_ID}`);
    expect(screen.getByRole("button", { name: "Create lead" })).toBeEnabled();
  });

  it("organisation-wide, an administrator must choose the owner", async () => {
    const api = mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/all/leads/duplicates": { status: 200, body: { results: [] } },
      "GET /api/v1/assignees": {
        status: 200,
        body: { results: [{ id: RAHUL_ID, full_name: "Rahul Sharma", email: "rahul@example.test" }], next: null, previous: null },
      },
      "POST /api/v1/workspaces/all/leads": { status: 201, body: makeLead() },
    });
    renderWithProviders(<NewLeadView />, { viewer: adminViewer });
    await ready();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("First name"), "Asha");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    expect(await screen.findByText("Choose who owns this lead.")).toBeInTheDocument();
    const owner = screen.getByRole("combobox", { name: "Owner" });
    await waitFor(() => expect(owner).toBeEnabled());
    await user.selectOptions(owner, RAHUL_ID);
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/all/leads")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/workspaces/all/leads")[0]!.body).toMatchObject({ owner: RAHUL_ID });
  });

  it("in a user's workspace, the lead is theirs and no owner is sent", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/new`;
    const api = mockApi({
      ...OPTIONS,
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: {
        status: 200,
        body: { kind: "user", subject: { id: RAHUL_ID, full_name: "Rahul Sharma", status: "active" } },
      },
      [`GET /api/v1/workspaces/${RAHUL_ID}/leads/duplicates`]: { status: 200, body: { results: [] } },
      [`POST /api/v1/workspaces/${RAHUL_ID}/leads`]: { status: 201, body: makeLead() },
    });
    renderWithProviders(<NewLeadView />, { viewer: adminViewer });
    await ready();
    expect(await screen.findByText("Rahul Sharma")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Owner" })).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/^Organization/), "Apollo");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`));
    expect(api.callsTo("POST", `/api/v1/workspaces/${RAHUL_ID}/leads`)[0]!.body).not.toHaveProperty("owner");
  });
});

describe("editing a lead", () => {
  const ME = `/api/v1/workspaces/me/leads/${LEAD_ID}`;

  beforeEach(() => {
    nav.pathname = `/leads/${LEAD_ID}/edit`;
  });

  it("sends only the fields that changed, with the version it loaded", async () => {
    const api = mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      [`GET ${ME}`]: { status: 200, body: makeLead() },
      [`PATCH ${ME}`]: { status: 200, body: makeLead({ city: "Pune", version: 4 }) },
    });
    renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await ready();
    const user = userEvent.setup();
    const city = await screen.findByLabelText(/^City/);
    await user.clear(city);
    await user.type(city, "Pune");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/leads/${LEAD_ID}`));
    expect(api.callsTo("PATCH", ME)[0]!.body).toEqual({ version: 3, city: "Pune" });
  });

  it("recovers from an edit conflict by re-applying my changes to the latest version", async () => {
    let serverLead = makeLead();
    const api = mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      [`GET ${ME}`]: () => ({ status: 200, body: serverLead }),
      [`PATCH ${ME}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === serverLead.version
          ? { status: 200, body: { ...serverLead, city: "Pune", version: serverLead.version + 1 } }
          : apiError(409, "conflict", "The record was changed by someone else."),
    });
    renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await ready();
    const user = userEvent.setup();
    const city = await screen.findByLabelText(/^City/);
    await user.clear(city);
    await user.type(city, "Pune");
    // Meanwhile someone else changes the city and the job title.
    serverLead = makeLead({ city: "Nashik", job_title: "Head of Lab", version: 4 });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    const alert = await screen.findByText("Someone else changed this lead while you were editing");
    expect(alert).toBeInTheDocument();
    expect(screen.getByText(/Nothing was overwritten/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply my changes to the latest version" }));
    expect(screen.getByLabelText(/^City/)).toHaveValue("Pune"); // mine kept
    expect(screen.getByLabelText(/^Job title/)).toHaveValue("Head of Lab"); // theirs kept
    expect(screen.getByText(/also changed by someone else/)).toHaveTextContent("City");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/leads/${LEAD_ID}`));
    expect(api.callsTo("PATCH", ME).map((c) => c.body)).toEqual([
      { version: 3, city: "Pune" },
      { version: 4, city: "Pune" },
    ]);
  });

  it("can discard my changes after a conflict", async () => {
    let serverLead = makeLead();
    mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      [`GET ${ME}`]: () => ({ status: 200, body: serverLead }),
      [`PATCH ${ME}`]: apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await ready();
    const user = userEvent.setup();
    const city = await screen.findByLabelText(/^City/);
    await user.clear(city);
    await user.type(city, "Pune");
    serverLead = makeLead({ city: "Nashik", version: 4 });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(await screen.findByRole("button", { name: "Discard my changes" }));
    expect(screen.getByLabelText(/^City/)).toHaveValue("Nashik");
  });

  it("an archived lead can't be edited here", async () => {
    mockApi({ ...OPTIONS, [`GET ${ME}`]: { status: 200, body: makeLead({ archived_at: "2026-09-29T10:00:00Z" }) } });
    renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByText("This lead is archived")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  });

  it("someone else's lead is simply not found", async () => {
    mockApi({ ...OPTIONS, [`GET ${ME}`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("collapsed sections open to show a server error inside them", async () => {
    mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      [`GET ${ME}`]: { status: 200, body: makeLead({ city: "", country: "" }) },
      [`PATCH ${ME}`]: apiError(400, "validation_error", "Some fields are invalid.", { description: ["Too long."] }),
    });
    renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await ready();
    const additional = screen.getByRole("region", { name: "Additional information" });
    expect(within(additional).getByRole("button", { name: "Additional information" })).toHaveAttribute("aria-expanded", "false");
    const user = userEvent.setup();
    await user.click(within(additional).getByRole("button", { name: "Additional information" }));
    await user.type(screen.getByLabelText(/^Description/), "x");
    await user.click(within(additional).getByRole("button", { name: "Additional information" })); // collapse again
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Too long.")).toBeVisible();
  });
});
