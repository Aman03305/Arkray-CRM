/**
 * Regression tests for the Phase 2 adversarial frontend review: one per confirmed finding
 * (F1-F10) plus the suspicions that were cheap to rule out, so none can silently return.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditLeadView, LeadView, LeadsView, NewLeadView } from "@/features/workspace/views";
import { fromBusinessDateTimeInput } from "@/lib/format";
import {
  adminViewer,
  LEAD_ID,
  LEAD_OPTIONS,
  makeLead,
  makeLeadListItem,
  RAHUL_ID,
  salesViewer,
} from "@/test/fixtures";
import { apiError, createTestQueryClient, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { RatingLabel, telHref } from "./LeadBits";
import { forgetLeadListState } from "./list-state";

const nav = vi.hoisted(() => ({ pathname: "/leads", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const OPTIONS = { "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS } };
const NO_DUPLICATES = { "GET /api/v1/workspaces/me/leads/duplicates": { status: 200, body: { results: [] } } };
const ME_LEAD = `/api/v1/workspaces/me/leads/${LEAD_ID}`;
const page = (results: unknown[], next: string | null = null) => ({ status: 200, body: { results, next, previous: null } });

beforeEach(() => {
  nav.pathname = "/leads";
  nav.push.mockReset();
  forgetLeadListState();
});

describe("F1: archive from the list after a conflict", () => {
  it("retries with the version the refreshed list shows", async () => {
    let listVersion = 3;
    const api = mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/me/leads": () => page([makeLeadListItem({ version: listVersion })]),
      [`POST ${ME_LEAD}/archive`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 4
          ? { status: 200, body: makeLead({ version: 5, archived_at: "2026-09-30T00:00:00Z" }) }
          : apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Asha Mehta" }));
    await user.click(screen.getByRole("menuitem", { name: "Archive" }));
    listVersion = 4; // someone else saved meanwhile
    await user.click(screen.getByRole("button", { name: "Archive lead" }));
    await screen.findByText(/The list has been refreshed; try again/);
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/workspaces/me/leads").length).toBeGreaterThan(1));
    await user.click(screen.getByRole("button", { name: "Archive lead" }));
    expect(await screen.findByText("Asha Mehta was archived.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME_LEAD}/archive`).map((c) => c.body)).toEqual([{ version: 3 }, { version: 4 }]);
  });
});

describe("F2: a conflict whose reload fails", () => {
  it("keeps the form and the unsaved edits on screen", async () => {
    let gets = 0;
    mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      [`GET ${ME_LEAD}`]: () => {
        gets += 1;
        return gets === 1 ? { status: 200, body: makeLead() } : apiError(503, "service_unavailable", "Down.");
      },
      [`PATCH ${ME_LEAD}`]: apiError(409, "conflict", "Changed."),
    });
    nav.pathname = `/leads/${LEAD_ID}/edit`;
    renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await screen.findByRole("option", { name: "Referral" });
    const user = userEvent.setup();
    const city = await screen.findByLabelText(/^City/);
    await user.clear(city);
    await user.type(city, "Pune - my unsaved edit");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText(/latest version couldn't be loaded here/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^City/)).toHaveValue("Pune - my unsaved edit");
    expect(screen.queryByText("This lead couldn't be loaded")).not.toBeInTheDocument();
  });
});

describe("F3: the owner picker never submits a user it doesn't show", () => {
  it("keeps the chosen user selected and visible after the search changes", async () => {
    const zed = { id: "77777777-7777-4777-8777-777777777777", full_name: "Zed Ali", email: "zed@example.test" };
    const rahul = { id: RAHUL_ID, full_name: "Rahul Sharma", email: "rahul@example.test" };
    const api = mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/all/leads/duplicates": { status: 200, body: { results: [] } },
      "GET /api/v1/assignees": (call: RecordedCall) => {
        const q = call.query.get("q");
        if (q === "ze") return page([zed]);
        if (q === "ra") return page([rahul]);
        return page([rahul], "http://x/api/v1/assignees?cursor=more"); // more than 100 users
      },
      "POST /api/v1/workspaces/all/leads": { status: 201, body: makeLead() },
    });
    renderWithProviders(<NewLeadView />, { viewer: adminViewer });
    await screen.findByRole("option", { name: "Referral" });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("First name"), "Asha");
    const find = await screen.findByRole("searchbox", { name: "Find a user" });
    await user.type(find, "ze");
    const owner = screen.getByRole("combobox", { name: "Owner" });
    await screen.findByRole("option", { name: /Zed Ali/ });
    await user.selectOptions(owner, zed.id);
    await user.clear(find);
    await user.type(find, "ra");
    await screen.findByRole("option", { name: /Rahul Sharma/ });
    expect(owner).toHaveValue(zed.id); // still shown as chosen ...
    expect(within(owner).getByRole("option", { name: /Zed Ali/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/all/leads")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/workspaces/all/leads")[0]!.body).toMatchObject({ owner: zed.id }); // ... and sent
  });
});

describe("F4: idempotency keys follow the request, not just the fields", () => {
  it("a different status after a failed attempt is a new key", async () => {
    const api = mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      "POST /api/v1/workspaces/me/leads": apiError(503, "service_unavailable", "Down."),
    });
    nav.pathname = "/leads/new";
    renderWithProviders(<NewLeadView />, { viewer: salesViewer });
    await screen.findByRole("option", { name: "Referral" });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("First name"), "Asha");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/me/leads")).toHaveLength(1));
    await user.selectOptions(screen.getByLabelText("Status"), "contacted");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/me/leads")).toHaveLength(2));
    const [first, second] = api.callsTo("POST", "/api/v1/workspaces/me/leads");
    expect((second!.body as { status: string }).status).toBe("contacted");
    expect(first!.headers["Idempotency-Key"]).not.toBe(second!.headers["Idempotency-Key"]);
  });
});

describe("F5: other cached copies of a changed lead are refreshed", () => {
  it("a lead archived organisation-wide isn't shown stale under a user's workspace", async () => {
    let archived = false;
    const api = mockApi({
      ...OPTIONS,
      [`GET /api/v1/workspaces/${RAHUL_ID}/leads/${LEAD_ID}`]: () => ({
        status: 200,
        body: makeLead({ archived_at: archived ? "2026-09-30T00:00:00Z" : null }),
      }),
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: {
        status: 200,
        body: { kind: "user", subject: { id: RAHUL_ID, full_name: "Rahul Sharma", status: "active" } },
      },
      [`GET /api/v1/workspaces/all/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      [`POST /api/v1/workspaces/all/leads/${LEAD_ID}/archive`]: () => {
        archived = true;
        return { status: 200, body: makeLead({ archived_at: "2026-09-30T00:00:00Z", version: 4 }) };
      },
    });
    const client = createTestQueryClient();
    client.setDefaultOptions({ queries: { retry: false, gcTime: Infinity, staleTime: 30_000 } });
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    const first = renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer, client });
    await screen.findByRole("heading", { level: 1, name: "Asha Mehta" });
    first.unmount();
    nav.pathname = `/leads/${LEAD_ID}`;
    const second = renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer, client });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    await user.click(screen.getByRole("button", { name: "Archive lead" }));
    await screen.findByText("Lead archived.");
    second.unmount();
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer, client });
    expect(await screen.findByText("This lead is archived")).toBeInTheDocument();
    expect(api.callsTo("GET", `/api/v1/workspaces/${RAHUL_ID}/leads/${LEAD_ID}`)).toHaveLength(2);
  });
});

describe("F6: a save that finishes after leaving the form", () => {
  it("doesn't navigate back or leave a notice behind", async () => {
    let release: () => void = () => {};
    mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      [`GET ${ME_LEAD}`]: { status: 200, body: makeLead() },
      [`PATCH ${ME_LEAD}`]: () =>
        new Promise((resolve) => {
          release = () => resolve({ status: 200, body: makeLead({ city: "Pune", version: 4 }) });
        }),
      "GET /api/v1/workspaces/me/leads": page([makeLeadListItem()]),
    });
    nav.pathname = `/leads/${LEAD_ID}/edit`;
    const form = renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await screen.findByRole("option", { name: "Referral" });
    const user = userEvent.setup();
    const city = await screen.findByLabelText(/^City/);
    await user.clear(city);
    await user.type(city, "Pune");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    form.unmount(); // the user left
    release();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(nav.push).not.toHaveBeenCalled();
    nav.pathname = "/leads";
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    await screen.findByRole("table", { name: "Leads" });
    expect(screen.queryByText("Changes saved.")).not.toBeInTheDocument();
  });
});

describe("F7: tap-to-call keeps extensions separate", () => {
  it.each([
    ["+1 555 010 9999 ext. 12", "tel:+15550109999;ext=12"],
    ["022 2345 6789 #45", "tel:02223456789;ext=45"],
    ["+91 98765 43210", "tel:+919876543210"],
  ])("%s -> %s", (typed, href) => {
    expect(telHref(typed)).toBe(href);
  });
});

describe("F8: focus never drops to the page", () => {
  it("after archiving from a row menu it moves to the notice", async () => {
    mockApi({
      ...OPTIONS,
      "GET /api/v1/workspaces/me/leads": page([makeLeadListItem()]),
      [`POST ${ME_LEAD}/archive`]: { status: 200, body: makeLead({ archived_at: "2026-09-30T00:00:00Z" }) },
    });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Asha Mehta" }));
    await user.click(screen.getByRole("menuitem", { name: "Archive" }));
    await user.click(screen.getByRole("button", { name: "Archive lead" }));
    await screen.findByText("Asha Mehta was archived.");
    await waitFor(() => expect(document.activeElement).not.toBe(document.body));
    expect(document.activeElement).toContainElement(screen.getByText("Asha Mehta was archived."));
  });

  it("after clearing filters it moves to the search box", async () => {
    mockApi({ ...OPTIONS, "GET /api/v1/workspaces/me/leads": page([makeLeadListItem()]) });
    renderWithProviders(<LeadsView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.type(await screen.findByRole("searchbox", { name: "Search leads" }), "asha");
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByRole("searchbox", { name: "Search leads" })).toHaveFocus();
  });

  it("after resolving an edit conflict it moves to the field to review", async () => {
    let serverLead = makeLead();
    mockApi({
      ...OPTIONS,
      ...NO_DUPLICATES,
      [`GET ${ME_LEAD}`]: () => ({ status: 200, body: serverLead }),
      [`PATCH ${ME_LEAD}`]: apiError(409, "conflict", "Changed."),
    });
    nav.pathname = `/leads/${LEAD_ID}/edit`;
    renderWithProviders(<EditLeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await screen.findByRole("option", { name: "Referral" });
    const user = userEvent.setup();
    const city = await screen.findByLabelText(/^City/);
    await user.clear(city);
    await user.type(city, "Pune");
    serverLead = makeLead({ city: "Nashik", version: 4 });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    const apply = await screen.findByRole("button", { name: "Apply my changes to the latest version" });
    await waitFor(() => expect(apply).toHaveFocus()); // offered to keyboard users at once
    await user.click(apply);
    await waitFor(() => expect(screen.getByLabelText(/^City/)).toHaveFocus());
  });
});

describe("F9: dialogs start focus in the right place", () => {
  it("change status focuses the first choosable status when the current one is retired", async () => {
    const options = {
      ...LEAD_OPTIONS,
      statuses: LEAD_OPTIONS.statuses.map((s) => (s.key === "new" ? { ...s, is_active: false, is_default: false } : s)),
    };
    mockApi({
      "GET /api/v1/config/lead-options": { status: 200, body: options },
      [`GET ${ME_LEAD}`]: { status: 200, body: makeLead() },
    });
    nav.pathname = `/leads/${LEAD_ID}`;
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await screen.findByRole("heading", { level: 1, name: "Asha Mehta" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Change status" })).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Change status" }));
    const dialog = await screen.findByRole("dialog", { name: "Change status" });
    await waitFor(() => expect(within(dialog).getByRole("radio", { name: "Contacted" })).toHaveFocus());
  });

  it("reassign focuses the owner picker", async () => {
    nav.pathname = `/leads/${LEAD_ID}`;
    mockApi({
      ...OPTIONS,
      [`GET /api/v1/workspaces/all/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      "GET /api/v1/assignees": page([]),
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Reassign" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "New owner" })).toHaveFocus());
  });
});

describe("F10: owner errors are announced and focused", () => {
  it("in a user's workspace the owner error is an alert", async () => {
    // The user was active when the form opened and was deactivated before it was saved.
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/new`;
    const message = "This user's account isn't active, so new leads can't be added to their workspace.";
    mockApi({
      ...OPTIONS,
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: {
        status: 200,
        body: { kind: "user", subject: { id: RAHUL_ID, full_name: "Rahul Sharma", status: "active" } },
      },
      [`GET /api/v1/workspaces/${RAHUL_ID}/leads/duplicates`]: { status: 200, body: { results: [] } },
      [`POST /api/v1/workspaces/${RAHUL_ID}/leads`]: apiError(400, "validation_error", "Some fields are invalid.", {
        owner: [message],
      }),
    });
    renderWithProviders(<NewLeadView />, { viewer: adminViewer });
    await screen.findByRole("option", { name: "Referral" });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("First name"), "Asha");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });

  it("reassigning without a choice focuses the picker", async () => {
    nav.pathname = `/leads/${LEAD_ID}`;
    mockApi({
      ...OPTIONS,
      [`GET /api/v1/workspaces/all/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      "GET /api/v1/assignees": page([]),
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Reassign" }));
    await user.click(screen.getByRole("button", { name: "Reassign lead" }));
    const select = screen.getByRole("combobox", { name: "New owner" });
    await waitFor(() => expect(select).toHaveFocus());
    expect(select).toHaveAccessibleDescription("Choose who should own this lead.");
  });
});

describe("suspicions ruled out", () => {
  it("a date-time value with seconds is read, not cleared", () => {
    expect(fromBusinessDateTimeInput("2026-09-30T10:00:30")).toBe("2026-09-30T04:30:30.000Z");
    expect(fromBusinessDateTimeInput("2026-13-40T10:00")).toBeNull();
  });

  it("an unknown rating renders as text instead of crashing", () => {
    render(<RatingLabel rating={"lukewarm" as never} />);
    expect(screen.getByText("lukewarm")).toBeInTheDocument();
  });
});
