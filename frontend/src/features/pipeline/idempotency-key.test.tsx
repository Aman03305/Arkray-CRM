/**
 * R103: the New Opportunity panel's Idempotency-Key (required by the server). One key per
 * logical submission: the same key for every retry of the same request, a new key for a
 * different request or a new panel, and none left over after a successful create.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { isIdempotencyKey } from "@/lib/random";
import type { Workspace } from "@/lib/workspace";
import { salesViewer } from "@/test/fixtures";
import { makeOpportunity, PIPELINE } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { OpportunityDrawer } from "./OpportunityDrawer";

vi.mock("next/navigation", () => ({
  usePathname: () => "/pipeline",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const ME: Workspace = { kind: "self" };
const CREATE = "/api/v1/workspaces/me/opportunities";
const CREATED = makeOpportunity({ title: "XYZ Laboratory", customer_name: "XYZ Laboratory" });
type Reply = Parameters<typeof mockApi>[0][string];

function routes(create: Reply) {
  return mockApi({
    "GET /api/v1/workspaces/me/pipelines": { status: 200, body: { results: [PIPELINE] } },
    "GET /api/v1/config/opportunity-options": { status: 200, body: { instruments: [{ name: "Adams 8180 T" }] } },
    "GET /api/v1/workspaces/me/leads/duplicates": { status: 200, body: { results: [] } },
    [`POST ${CREATE}`]: create,
  });
}

/** Replies in turn (the last one repeats). */
function sequence(...replies: (() => Promise<{ status: number; body?: unknown }> | { status: number; body?: unknown })[]): Reply {
  let call = 0;
  return async () => replies[Math.min(call++, replies.length - 1)]!();
}

const created = () => ({ status: 201, body: CREATED });
const unavailable = () => apiError(503, "service_unavailable", "Unavailable.");

async function openPanel(onSaved = vi.fn(), onClose = vi.fn()) {
  const view = renderWithProviders(<OpportunityDrawer workspace={ME} opportunity={null} pipelineId={PIPELINE.id} onClose={onClose} onSaved={onSaved} />, {
    viewer: salesViewer,
  });
  const user = userEvent.setup();
  const panel = await screen.findByRole("dialog", { name: "New opportunity" });
  await within(panel).findByRole("radiogroup", { name: "Instrument name (optional)" });
  await waitFor(() => expect(within(panel).getByRole("combobox", { name: "Pipeline" })).toHaveValue(PIPELINE.id));
  return { user, panel, view };
}

async function fill(user: ReturnType<typeof userEvent.setup>, panel: HTMLElement, customer = "XYZ Laboratory") {
  await user.type(within(panel).getByLabelText("Customer name"), customer);
  await user.type(within(panel).getByLabelText("Installation price (₹)"), "100000");
}

const createButton = (panel: HTMLElement) => within(panel).getByRole("button", { name: "Create opportunity" });
const keys = (api: ReturnType<typeof mockApi>) => api.callsTo("POST", CREATE).map((c) => c.headers["Idempotency-Key"]);

describe("the New opportunity panel's idempotency key", () => {
  it("is a UUID, and the same for every retry of the same request (503, 429, 500, network error)", async () => {
    const api = routes(
      sequence(
        unavailable,
        () => apiError(429, "throttled", "Too many requests."),
        () => apiError(500, "server_error", "Oops."),
        () => {
          // No answer at all (the request may still have been applied, as after a timeout).
          throw new TypeError("Failed to fetch");
        },
        created,
      ),
    );
    const onSaved = vi.fn();
    const { user, panel } = await openPanel(onSaved);
    await fill(user, panel);
    for (let attempt = 1; attempt <= 5; attempt += 1) {
      await waitFor(() => expect(createButton(panel)).toBeEnabled());
      await user.click(createButton(panel));
      await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(attempt));
    }
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const sent = keys(api);
    expect(isIdempotencyKey(sent[0])).toBe(true);
    expect(new Set(sent).size).toBe(1);
  });

  it("is new when the request changes, and a key is never sent with two different bodies", async () => {
    const api = routes(sequence(unavailable, unavailable, created));
    const { user, panel } = await openPanel();
    await fill(user, panel);
    await user.click(createButton(panel));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(1));
    await waitFor(() => expect(createButton(panel)).toBeEnabled());
    await user.type(within(panel).getByLabelText("Account name"), "XYZ Group");
    await user.click(createButton(panel));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(2));
    await waitFor(() => expect(createButton(panel)).toBeEnabled());
    await user.click(createButton(panel)); // the corrected request again: its key again
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(3));
    const [first, second, third] = keys(api);
    expect(second).not.toBe(first);
    expect(third).toBe(second);
    const bodies = new Map<string, string>();
    for (const call of api.callsTo("POST", CREATE)) {
      const body = JSON.stringify(call.body);
      const key = call.headers["Idempotency-Key"]!;
      expect(bodies.get(key) ?? body).toBe(body);
      bodies.set(key, body);
    }
  });

  it("is the same after a refused (400) request is retried unchanged, and new once it is corrected", async () => {
    const api = routes(
      sequence(
        () => apiError(400, "validation_error", "Some fields are invalid.", { contact_email: ["Enter a valid email address."] }),
        () => apiError(400, "validation_error", "Some fields are invalid.", { contact_email: ["Enter a valid email address."] }),
        created,
      ),
    );
    const { user, panel } = await openPanel();
    await fill(user, panel);
    await user.type(within(panel).getByLabelText("Email (optional)"), "a@b.example");
    await user.click(createButton(panel));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(1));
    await waitFor(() => expect(createButton(panel)).toBeEnabled());
    await user.click(createButton(panel));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(2));
    await waitFor(() => expect(createButton(panel)).toBeEnabled());
    await user.clear(within(panel).getByLabelText("Email (optional)"));
    await user.type(within(panel).getByLabelText("Email (optional)"), "lab@xyz.example");
    await user.click(createButton(panel));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(3));
    const [first, second, third] = keys(api);
    expect(second).toBe(first);
    expect(third).not.toBe(first);
  });

  it("does not survive a successful create: the panel takes no second submit, and the next panel has a new key", async () => {
    const api = routes(created);
    const onSaved = vi.fn(); // the panel stays open (as it does until the page moves on)
    const first = await openPanel(onSaved);
    await fill(first.user, first.panel);
    await first.user.click(createButton(first.panel));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    await first.user.click(createButton(first.panel));
    fireEvent.submit(first.panel.querySelector("form")!);
    expect(api.callsTo("POST", CREATE)).toHaveLength(1);
    first.view.unmount();

    // The next New opportunity (or the same form after a reload or Back/Forward), with the
    // very same details: a new submission, so a new key and a second, separate opportunity.
    const second = await openPanel();
    await fill(second.user, second.panel);
    await second.user.click(createButton(second.panel));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(2));
    const [a, b] = api.callsTo("POST", CREATE);
    expect(b!.body).toEqual(a!.body);
    expect(b!.headers["Idempotency-Key"]).not.toBe(a!.headers["Idempotency-Key"]);
  });

  it("differs between two panels open at once (two tabs), even for identical details", async () => {
    const api = routes(created);
    const one = await openPanel();
    await fill(one.user, one.panel);
    const two = renderWithProviders(<OpportunityDrawer workspace={ME} opportunity={null} pipelineId={PIPELINE.id} onClose={vi.fn()} onSaved={vi.fn()} />, {
      viewer: salesViewer,
    });
    const panels = await screen.findAllByRole("dialog", { name: "New opportunity" });
    expect(panels).toHaveLength(2);
    const other = panels.find((p) => p !== one.panel)!;
    await within(other).findByRole("radiogroup", { name: "Instrument name (optional)" });
    await waitFor(() => expect(within(other).getByRole("combobox", { name: "Pipeline" })).toHaveValue(PIPELINE.id));
    fireEvent.change(within(other).getByLabelText("Customer name"), { target: { value: "XYZ Laboratory" } });
    fireEvent.change(within(other).getByLabelText("Installation price (₹)"), { target: { value: "100000" } });
    fireEvent.submit(one.panel.querySelector("form")!);
    fireEvent.submit(other.querySelector("form")!);
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(2));
    const [a, b] = api.callsTo("POST", CREATE);
    expect(b!.body).toEqual(a!.body);
    expect(b!.headers["Idempotency-Key"]).not.toBe(a!.headers["Idempotency-Key"]);
    two.unmount();
  });

  it("sends one request for two submits in the same moment (before the busy state renders)", async () => {
    const api = routes(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
      return created();
    });
    const onSaved = vi.fn();
    const { user, panel } = await openPanel(onSaved);
    await fill(user, panel);
    const form = panel.querySelector("form")!;
    // Both inside one act(): no render between them, as when two events arrive in one frame.
    act(() => {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(api.callsTo("POST", CREATE)).toHaveLength(1);
  });

  it("explains a refused key plainly (never expected) and uses a new key for the next attempt", async () => {
    const message = "Send an Idempotency-Key header: a new UUID for each new opportunity, the same one when retrying it.";
    const api = routes(
      sequence(
        () => apiError(400, "validation_error", message, { idempotency_key: [message] }),
        () => apiError(422, "idempotency_key_reused", "This request key was already used for a different request."),
        created,
      ),
    );
    const onSaved = vi.fn();
    const { user, panel } = await openPanel(onSaved);
    await fill(user, panel);
    await user.click(createButton(panel));
    const alert = await within(panel).findByRole("alert");
    expect(alert).toHaveTextContent("This couldn't be sent. Please try again.");
    expect(alert).not.toHaveTextContent(/Idempotency/i);
    await user.click(createButton(panel));
    await waitFor(() => expect(api.callsTo("POST", CREATE)).toHaveLength(2));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("This couldn't be sent. Please try again.");
    await user.click(createButton(panel));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const [first, second, third] = keys(api);
    expect(second).not.toBe(first);
    expect(third).not.toBe(second);
  });
});
