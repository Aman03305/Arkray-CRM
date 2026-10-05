/**
 * ADR-0028: the New Opportunity panel. No name, no lead and no lead finder: the customer's
 * details, an instrument from the server's list (dragged, clicked or chosen by keyboard), the
 * deal's figures, the timeline and the pipeline. The server names the opportunity and makes
 * its lead; a retry or double click is one creation.
 */
import { createEvent, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PipelineView } from "@/features/workspace/views";
import { businessToday } from "@/lib/format";
import { salesViewer } from "@/test/fixtures";
import { makeBoard, makeOpportunity, PIPELINE, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { forgetBoardState } from "./hooks";
import { INSTRUMENT_DRAG_TYPE } from "./InstrumentPicker";

const nav = vi.hoisted(() => ({ pathname: "/pipeline", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const CREATE = "/api/v1/workspaces/me/opportunities";
const INSTRUMENTS = ["Adams 8380 V-lite", "Adams 8180 V", "Adams 8180 T", "PCBA with Printer"];
const LEAD_ID = "4d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a";
const CREATED = makeOpportunity({
  title: "ABC Diagnostics Mumbai — Adams 8380 V-lite",
  account_name: "ABC Diagnostics",
  customer_name: "ABC Diagnostics Mumbai",
  instrument_name: "Adams 8380 V-lite",
  expected_cpt: "Rs 18 per test",
  lead: { id: LEAD_ID, display_name: "ABC Diagnostics Mumbai", organization_name: "ABC Diagnostics", restricted: false },
});

function routes(extra: Record<string, Parameters<typeof mockApi>[0][string]> = {}) {
  return mockApi({
    "GET /api/v1/workspaces/me/pipelines": { status: 200, body: { results: [PIPELINE] } },
    "GET /api/v1/workspaces/me/pipeline-board": { status: 200, body: makeBoard([]) },
    "GET /api/v1/config/opportunity-options": { status: 200, body: { instruments: INSTRUMENTS.map((name) => ({ name })) } },
    "GET /api/v1/workspaces/me/leads/duplicates": { status: 200, body: { results: [] } },
    [`POST ${CREATE}`]: { status: 201, body: CREATED },
    ...extra,
  });
}

async function openPanel() {
  renderWithProviders(<PipelineView />, { viewer: salesViewer });
  const user = userEvent.setup();
  await user.click((await screen.findAllByRole("button", { name: "New opportunity" }))[0]!);
  const panel = screen.getByRole("dialog", { name: "New opportunity" });
  await within(panel).findByRole("radiogroup", { name: "Instrument name (optional)" });
  return { user, panel };
}

function dragInstrument(panel: HTMLElement, name: string) {
  const store: Record<string, string> = {};
  const dataTransfer = {
    get types() {
      return Object.keys(store);
    },
    setData: (type: string, value: string) => {
      store[type] = value;
    },
    getData: (type: string) => store[type] ?? "",
    dropEffect: "none",
    effectAllowed: "all",
  };
  const fire = (element: Element, type: "dragStart" | "dragOver" | "drop") => {
    const event = createEvent[type](element);
    Object.defineProperty(event, "dataTransfer", { value: dataTransfer });
    fireEvent(element, event);
  };
  fire(within(panel).getByRole("radio", { name }), "dragStart");
  const zone = within(panel).getByTestId("instrument-drop-zone");
  fire(zone, "dragOver");
  fire(zone, "drop");
  expect(store[INSTRUMENT_DRAG_TYPE]).toBe(name);
}

async function fillScenario(user: ReturnType<typeof userEvent.setup>, panel: HTMLElement) {
  await user.type(within(panel).getByLabelText("Account name"), "ABC Diagnostics");
  await user.type(within(panel).getByLabelText("Customer name"), "ABC Diagnostics Mumbai");
  await user.type(within(panel).getByLabelText("Contact (optional)"), "9876543210");
  await user.type(within(panel).getByLabelText("Address (optional)"), "Mumbai");
  await user.type(within(panel).getByLabelText("Work load (optional)"), "300 tests/day");
  await user.type(within(panel).getByLabelText("Installation price (₹)"), "850000");
  await user.type(within(panel).getByLabelText("Expected CPT (optional)"), "Rs 18 per test");
  fireEvent.change(within(panel).getByLabelText("Expected closing date (optional)"), { target: { value: "2026-12-15" } });
}

beforeEach(() => {
  nav.pathname = "/pipeline";
  forgetBoardState();
});

describe("the New opportunity panel", () => {
  it("asks for no opportunity name, lead or lead search, and has the approved fields in order", async () => {
    routes();
    const { panel } = await openPanel();
    expect(within(panel).queryByLabelText(/opportunity name/i)).not.toBeInTheDocument();
    expect(within(panel).queryByText(/find a lead/i)).not.toBeInTheDocument();
    expect(within(panel).queryByRole("combobox", { name: /lead/i })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("searchbox")).not.toBeInTheDocument();
    expect(within(panel).queryByLabelText(/^lead/i)).not.toBeInTheDocument();
    // Short section headings, in the approved order (no custom fields here: no Additional).
    expect(within(panel).getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "Customer",
      "Instrument",
      "Timeline",
      "Pipeline",
    ]);
    const labels = [
      "Account name",
      "Customer name",
      "Contact (optional)",
      "Address (optional)",
      "Work load (optional)",
      "Installation price (₹)",
      "Expected CPT (optional)",
      "Opportunity date",
      "Expected closing date (optional)",
    ];
    for (const label of labels) expect(within(panel).getByLabelText(label)).toBeInTheDocument();
    expect(within(panel).getByRole("combobox", { name: "Pipeline" })).toHaveValue(PIPELINE.id);
    expect(within(panel).getByRole("combobox", { name: "Stage" })).toHaveValue(STAGES.new.id);
    const fields = within(panel).getAllByRole("textbox").concat(within(panel).getAllByRole("combobox"));
    expect(fields.map((f) => f.getAttribute("name"))).not.toContain("title");
    expect(within(panel).getAllByRole("radio").map((r) => r.textContent)).toEqual(INSTRUMENTS);
    // A new opportunity takes its stage's probability: no own-probability or description here.
    expect(within(panel).queryByLabelText(/probability/i)).not.toBeInTheDocument();
    expect(within(panel).queryByLabelText(/description/i)).not.toBeInTheDocument();
    expect(within(panel).getByLabelText("Opportunity date")).toHaveValue(businessToday());
    expect(within(panel).getByRole("button", { name: "Create opportunity" })).toBeInTheDocument();
  });

  it("is a panel on the right: the board stays behind it on wide screens", async () => {
    routes();
    const { panel } = await openPanel();
    expect(panel.className).toContain("sm:max-w-2xl"); // bounded width from the small breakpoint
    expect(panel.parentElement?.className).toContain("justify-end"); // against the right edge
    expect(panel.className).toContain("w-full"); // phones: the whole width
    expect(screen.getByRole("heading", { level: 1, name: "Pipeline" })).toBeInTheDocument();
  });

  it("creates from the scenario's details with a dragged instrument: no name, no lead, one key", async () => {
    const api = routes();
    const { user, panel } = await openPanel();
    await fillScenario(user, panel);
    dragInstrument(panel, "Adams 8380 V-lite");
    expect(within(panel).getByRole("radio", { name: "Adams 8380 V-lite" })).toHaveAttribute("aria-checked", "true");
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(1));
    const [call] = api.callsTo("POST", CREATE);
    expect(call!.body).toEqual({
      value: "850000",
      opportunity_date: businessToday(),
      account_name: "ABC Diagnostics",
      customer_name: "ABC Diagnostics Mumbai",
      contact_phone: "9876543210",
      address: "Mumbai",
      instrument_name: "Adams 8380 V-lite",
      work_load: "300 tests/day",
      expected_cpt: "Rs 18 per test",
      expected_close_date: "2026-12-15",
      pipeline: PIPELINE.id,
    });
    expect(call!.headers["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
    // The server's name for it; the panel closes.
    expect(await screen.findByText("“ABC Diagnostics Mumbai — Adams 8380 V-lite” created.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "New opportunity" })).not.toBeInTheDocument();
  });

  it.each(INSTRUMENTS)("sends %s chosen with a click", async (instrument) => {
    const api = routes();
    const { user, panel } = await openPanel();
    await user.type(within(panel).getByLabelText("Customer name"), "XYZ Laboratory");
    await user.type(within(panel).getByLabelText("Installation price (₹)"), "100000");
    await user.click(within(panel).getByRole("radio", { name: instrument }));
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(1));
    expect(api.callsTo("POST", CREATE)[0]!.body).toMatchObject({ instrument_name: instrument, customer_name: "XYZ Laboratory" });
  });

  it("chooses into a stage of the chosen pipeline", async () => {
    const api = routes();
    const { user, panel } = await openPanel();
    await user.type(within(panel).getByLabelText("Customer name"), "XYZ Laboratory");
    await user.type(within(panel).getByLabelText("Installation price (₹)"), "100000");
    await user.selectOptions(within(panel).getByLabelText("Stage"), STAGES.qualified.id);
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(1));
    expect(api.callsTo("POST", CREATE)[0]!.body).toMatchObject({ pipeline: PIPELINE.id, stage: STAGES.qualified.id });
  });

  it("needs only the customer or the account name", async () => {
    const api = routes();
    const { user, panel } = await openPanel();
    await user.type(within(panel).getByLabelText("Installation price (₹)"), "100000");
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    expect(within(panel).getByLabelText("Customer name")).toHaveAccessibleDescription("Enter the customer name or the account name.");
    expect(api.callsTo("POST", CREATE)).toHaveLength(0);
    await user.type(within(panel).getByLabelText("Account name"), "ABC Diagnostics");
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(1));
    const body = api.callsTo("POST", CREATE)[0]!.body as Record<string, unknown>;
    expect(body.account_name).toBe("ABC Diagnostics");
    expect(body).not.toHaveProperty("customer_name");
  });

  it("a double click creates once, and a retry after a failure is the same creation (same key)", async () => {
    let attempt = 0;
    const api = routes({
      [`POST ${CREATE}`]: async () => {
        attempt += 1;
        if (attempt === 1) {
          await new Promise((resolve) => setTimeout(resolve, 50));
          return apiError(503, "service_unavailable", "Unavailable.");
        }
        return { status: 201, body: CREATED };
      },
    });
    const { user, panel } = await openPanel();
    await fillScenario(user, panel);
    const create = within(panel).getByRole("button", { name: "Create opportunity" });
    await user.dblClick(create);
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(1));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("temporarily unavailable");
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(2));
    const [first, second] = api.callsTo("POST", CREATE);
    expect(second!.headers["Idempotency-Key"]).toBe(first!.headers["Idempotency-Key"]);
    expect(await screen.findByText("“ABC Diagnostics Mumbai — Adams 8380 V-lite” created.")).toBeInTheDocument();
  });

  it("warns of a possible existing lead with the same phone, without blocking or linking it", async () => {
    const api = routes({
      "GET /api/v1/workspaces/me/leads/duplicates": {
        status: 200,
        body: {
          results: [
            {
              id: LEAD_ID,
              display_name: "ABC Diagnostics Mumbai",
              organization_name: "ABC Diagnostics",
              owner: { id: salesViewer.id, full_name: "Priya Patel", is_active: true },
              archived_at: null,
              matched_on: ["phone"],
            },
          ],
        },
      },
    });
    const { user, panel } = await openPanel();
    await fillScenario(user, panel);
    expect(await within(panel).findByText("Possible existing lead", {}, { timeout: 3000 })).toBeInTheDocument();
    const [check] = api.callsTo("GET", "/api/v1/workspaces/me/leads/duplicates");
    expect(check!.query.getAll("phone")).toEqual(["9876543210"]);
    expect(within(panel).getByRole("link", { name: "ABC Diagnostics Mumbai" })).toHaveAttribute("href", `/leads/${LEAD_ID}`);
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(1));
    expect(api.callsTo("POST", CREATE)[0]!.body).not.toHaveProperty("lead");
  });

  it("shows the server's refusal of an instrument under the instruments", async () => {
    routes({
      [`POST ${CREATE}`]: apiError(400, "validation_error", "Some fields are invalid.", {
        instrument_name: ["Choose an instrument from the list."],
      }),
    });
    const { user, panel } = await openPanel();
    await fillScenario(user, panel);
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    const group = within(panel).getByRole("radiogroup", { name: "Instrument name (optional)" });
    await waitFor(() => expect(group).toHaveAttribute("aria-invalid", "true"));
    expect(group).toHaveAccessibleDescription("Choose an instrument from the list.");
    // Focus goes to the problem: the group's tab stop (the chosen instrument).
    await waitFor(() => expect(within(group).getByRole("radio", { name: "Adams 8380 V-lite" })).toHaveFocus());
  });
});
