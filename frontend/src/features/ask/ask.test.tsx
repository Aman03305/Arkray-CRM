/**
 * Ask Arkray in the browser: answers render as safe text with workspace-local record links,
 * pending questions are polled, failures degrade, the sidebar offers it only when allowed,
 * and nothing of Rahul's ever renders under Priya's workspace, even when Rahul's answer
 * arrives after the switch.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/shell/Sidebar";
import { AskWorkspaceView } from "@/features/workspace/views";
import type { AskAnswer, AskQuestion } from "@/lib/api/types";
import { adminViewer, makeViewer } from "@/test/fixtures";
import { apiError, createTestQueryClient, mockApi, renderWithProviders } from "@/test/render";

import { recordHref } from "./Answer";

const nav = vi.hoisted(() => ({ pathname: "/ask" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
}));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const RAHUL = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";
const PRIYA = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const LEAD = "11111111-1111-4111-8111-111111111111";
const NOTE = "22222222-2222-4222-8222-222222222222";
const DEAL = "33333333-3333-4333-8333-333333333333";
const TASK = "44444444-4444-4444-8444-444444444444";
const asker = makeViewer({ features: { ask: true } });
const admin = { ...adminViewer, features: { ask: true } };

function answer(overrides: Partial<AskAnswer> = {}): AskAnswer {
  return {
    blocks: [{ type: "paragraph", parts: [{ text: "Your pipeline value is " }, { text: "₹10,00,000", bold: true }, { text: "." }] }],
    facts: [{ label: "Pipeline value", value: "₹10,00,000", kind: "money", raw: "1000000.00" }],
    sources: [],
    citations: [],
    notices: [],
    provenance: { mode: "router", tools: ["get_pipeline_summary"], grounded: true, model: "" },
    ...overrides,
  };
}

function question(overrides: Partial<AskQuestion> = {}): AskQuestion {
  return {
    id: "q-1",
    conversation_id: "c-1",
    question: "What is my pipeline value?",
    status: "answered",
    error: null,
    answer: answer(),
    created_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    ...overrides,
  };
}

const STATUS = { status: 200, body: { enabled: true, summaries: "available" } };
const NONE = { status: 200, body: [] };

function routesFor(segment: string, extra: Parameters<typeof mockApi>[0] = {}): Parameters<typeof mockApi>[0] {
  return {
    [`GET /api/v1/workspaces/${segment}/ask`]: STATUS,
    [`GET /api/v1/workspaces/${segment}/ask/conversations`]: NONE,
    ...extra,
  };
}

function subject(id: string, name: string) {
  return { status: 200, body: { segment: id, kind: "user", subject: { id, full_name: name, status: "active", is_active: true } } };
}

async function askFor(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByRole("textbox"), text);
  await user.click(screen.getByRole("button", { name: "Ask" }));
}

describe("answers", () => {
  it("renders a routed answer with facts, badges and the scope", async () => {
    nav.pathname = "/ask";
    mockApi(routesFor("me", { "POST /api/v1/workspaces/me/ask": { status: 201, body: question() } }));
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    expect(await screen.findByText("Answers come only from your records.")).toBeInTheDocument();
    await askFor("What is my pipeline value?");
    const thread = await screen.findByRole("list", { name: "Questions and answers" });
    expect(within(thread).getByText("₹10,00,000", { selector: "strong" })).toBeInTheDocument();
    expect(within(thread).getByText("From your CRM")).toBeInTheDocument();
    expect(within(thread).getByText("Pipeline value")).toBeInTheDocument();
  });

  it("links sources inside the current workspace and never renders markup", async () => {
    nav.pathname = `/admin/users/${RAHUL}/ask`;
    const reply = question({
      answer: answer({
        blocks: [
          {
            type: "paragraph",
            parts: [
              { text: '<img src=x onerror="alert(1)"> see ' },
              { ref: `opportunity:${DEAL}` },
              { text: " for " },
              { ref: `lead:${LEAD}` },
              { ref: "note:99999999-9999-4999-8999-999999999999" },
            ],
          },
        ],
        sources: [
          { ref: `opportunity:${DEAL}`, kind: "opportunity", id: DEAL, label: "Analyser <i>upgrade</i>", detail: "" },
          { ref: `lead:${LEAD}`, kind: "lead", id: LEAD, label: "Dr Mehta <b>bold</b>", detail: "" },
          { ref: `note:${NOTE}`, kind: "note", id: NOTE, label: "Call notes", detail: "" },
        ],
        citations: [{ ref: `note:${NOTE}`, kind: "note", label: "Note", snippet: "Asked <script>x()</script> about price", when: "3 Oct 2026, 10:30 AM" }],
        provenance: { mode: "llm", tools: ["search_notes"], grounded: true, model: "m" },
      }),
    });
    mockApi({
      ...routesFor(RAHUL, { [`POST /api/v1/workspaces/${RAHUL}/ask`]: { status: 201, body: reply } }),
      [`GET /api/v1/workspaces/${RAHUL}`]: subject(RAHUL, "Rahul Sharma"),
    });
    const { container } = renderWithProviders(<AskWorkspaceView />, { viewer: admin });
    expect(await screen.findByText("Answers come only from Rahul Sharma's records.")).toBeInTheDocument();
    await askFor("Tell me about Dr Mehta");
    const thread = await screen.findByRole("list", { name: "Questions and answers" });
    expect(await within(thread).findByRole("link", { name: "Analyser <i>upgrade</i>" })).toHaveAttribute(
      "href",
      `/admin/users/${RAHUL}/pipeline/${DEAL}`,
    );
    expect(within(thread).getByRole("link", { name: "Call notes" })).toHaveAttribute("href", `/admin/users/${RAHUL}/activities/${NOTE}`);
    // The customer (the API's lead) has no page: named, never linked.
    expect(within(thread).getByText("Dr Mehta <b>bold</b>").closest("a")).toBeNull();
    expect(container.querySelector("img, script, b, i")).toBeNull();
    expect(screen.getByText(/<img src=x onerror="alert\(1\)"> see/)).toBeInTheDocument();
    // A reference the server didn't resolve is not rendered at all.
    expect(screen.getAllByRole("link").map((l) => l.getAttribute("href"))).not.toContain(
      `/admin/users/${RAHUL}/activities/99999999-9999-4999-8999-999999999999`,
    );
    expect(screen.getAllByRole("link").some((l) => l.getAttribute("href")?.includes("/leads"))).toBe(false);
    expect(screen.getByText("Asked <script>x()</script> about price")).toBeInTheDocument();
  });

  it("names a cited customer (the API's lead) as plain text labelled Customer, never as a link", async () => {
    nav.pathname = "/ask";
    const reply = question({
      answer: answer({
        blocks: [{ type: "paragraph", parts: [{ text: "They asked about price." }] }],
        sources: [
          { ref: `lead:${LEAD}`, kind: "lead", id: LEAD, label: "Dr Mehta", detail: "" },
          { ref: `note:${NOTE}`, kind: "note", id: NOTE, label: "Call notes", detail: "" },
        ],
        citations: [
          { ref: `lead:${LEAD}`, kind: "lead", label: "Dr Mehta", snippet: "Prefers morning calls", when: "" },
          { ref: `note:${NOTE}`, kind: "note", label: "Call notes", snippet: "Asked about price", when: "3 Oct 2026, 10:30 AM" },
        ],
        provenance: { mode: "llm", tools: ["search_notes"], grounded: true, model: "m" },
      }),
    });
    mockApi(routesFor("me", { "POST /api/v1/workspaces/me/ask": { status: 201, body: reply } }));
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await askFor("What did Dr Mehta ask?");
    const quote = (await screen.findByText("Prefers morning calls")).closest("li")!;
    expect(within(quote).getByText("Customer")).toBeInTheDocument();
    expect(within(quote).getByText("Dr Mehta")).toBeInTheDocument();
    expect(within(quote).queryByRole("link")).not.toBeInTheDocument();
    expect(quote).not.toHaveTextContent(/lead/i);
    // The note quoted next to it still opens in this workspace.
    const note = screen.getByText("Asked about price").closest("li")!;
    expect(within(note).getByText("Note")).toBeInTheDocument();
    expect(within(note).getByRole("link", { name: "Call notes" })).toHaveAttribute("href", `/activities/${NOTE}`);
  });

  it("lists a customer source as Customer without a link; opportunity and activity sources link in this workspace", async () => {
    nav.pathname = `/admin/users/${RAHUL}/ask`;
    const reply = question({
      answer: answer({
        sources: [
          { ref: `lead:${LEAD}`, kind: "lead", id: LEAD, label: "Dr Mehta", detail: "" },
          { ref: `opportunity:${DEAL}`, kind: "opportunity", id: DEAL, label: "Analyser upgrade", detail: "" },
          { ref: `task:${TASK}`, kind: "task", id: TASK, label: "Send the quote", detail: "" },
        ],
      }),
    });
    mockApi({
      ...routesFor(RAHUL, { [`POST /api/v1/workspaces/${RAHUL}/ask`]: { status: 201, body: reply } }),
      [`GET /api/v1/workspaces/${RAHUL}`]: subject(RAHUL, "Rahul Sharma"),
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: admin });
    await askFor("Which deals are open?");
    const heading = await screen.findByRole("heading", { name: "Sources" });
    const items = within(heading.parentElement!).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "Customer: Dr Mehta",
      "Opportunity: Analyser upgrade",
      "Task: Send the quote",
    ]);
    expect(within(items[0]!).queryByRole("link")).not.toBeInTheDocument();
    expect(within(items[1]!).getByRole("link", { name: "Analyser upgrade" })).toHaveAttribute("href", `/admin/users/${RAHUL}/pipeline/${DEAL}`);
    expect(within(items[2]!).getByRole("link", { name: "Send the quote" })).toHaveAttribute("href", `/admin/users/${RAHUL}/activities/${TASK}`);
  });

  it("recordHref gives a customer record no page, and keeps the others in the workspace", () => {
    const rahul = { kind: "user", userId: RAHUL } as const;
    expect(recordHref(rahul, "lead", LEAD)).toBeNull();
    expect(recordHref({ kind: "self" }, "lead", LEAD)).toBeNull();
    expect(recordHref(rahul, "opportunity", DEAL)).toBe(`/admin/users/${RAHUL}/pipeline/${DEAL}`);
    expect(recordHref(rahul, "meeting", TASK)).toBe(`/admin/users/${RAHUL}/activities/${TASK}`);
    expect(recordHref({ kind: "organization" }, "note", NOTE)).toBe(`/activities/${NOTE}`);
  });

  it("polls a pending question until it is answered", async () => {
    nav.pathname = "/ask";
    let polls = 0;
    mockApi(
      routesFor("me", {
        "POST /api/v1/workspaces/me/ask": { status: 201, body: question({ status: "pending", answer: null, finished_at: null }) },
        "GET /api/v1/workspaces/me/ask/questions/q-1": () => {
          polls += 1;
          return polls < 2
            ? { status: 200, body: question({ status: "pending", answer: null, finished_at: null }) }
            : { status: 200, body: question() };
        },
      }),
    );
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await askFor("What concerns did they raise?");
    expect(await screen.findByRole("status", { name: "" })).toHaveTextContent("Looking through the records");
    expect(await screen.findByText("₹10,00,000", { selector: "strong" }, { timeout: 4000 })).toBeInTheDocument();
    expect(polls).toBeGreaterThanOrEqual(2);
  });

  it("explains a failed question and keeps the suggestions working", async () => {
    nav.pathname = "/ask";
    mockApi(
      routesFor("me", {
        "POST /api/v1/workspaces/me/ask": {
          status: 201,
          body: question({ status: "failed", error: "ai_unavailable", answer: null }),
        },
      }),
    );
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await askFor("Why did we lose?");
    expect(await screen.findByRole("alert")).toHaveTextContent("Pipeline, customer, task and meeting questions");
  });

  it("explains a refused question (rate limit)", async () => {
    nav.pathname = "/ask";
    mockApi(routesFor("me", { "POST /api/v1/workspaces/me/ask": apiError(429, "rate_limited", "Too many.") }));
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await askFor("Hello?");
    expect(await screen.findByRole("alert")).toHaveTextContent("You're asking quickly");
  });

  it("says when Ask Arkray is turned off", async () => {
    nav.pathname = "/ask";
    mockApi({ "GET /api/v1/workspaces/me/ask": { status: 200, body: { enabled: false, summaries: "none" } }, "GET /api/v1/workspaces/me/ask/conversations": NONE });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    expect(await screen.findByText("Ask Arkray is turned off for this CRM.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("is not found for a viewer who may not ask", async () => {
    nav.pathname = "/ask";
    mockApi(routesFor("me"));
    renderWithProviders(<AskWorkspaceView />, { viewer: makeViewer({ features: { ask: true }, capabilities: ["crm.access_own"] }) });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});

describe("conversations", () => {
  it("opens a recent conversation and forgets it", async () => {
    nav.pathname = "/ask";
    const api = mockApi(
      routesFor("me", {
        "GET /api/v1/workspaces/me/ask/conversations": { status: 200, body: [{ id: "c-1", title: "What is my pipeline value?", created_at: "", updated_at: "" }] },
        "GET /api/v1/workspaces/me/ask/conversations/c-1": { status: 200, body: { id: "c-1", created_at: "", updated_at: "", questions: [question()] } },
        "DELETE /api/v1/workspaces/me/ask/conversations/c-1": { status: 204 },
      }),
    );
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "What is my pipeline value?" }));
    expect(await screen.findByText("₹10,00,000", { selector: "strong" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Forget" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Forget this conversation?" });
    await user.click(within(dialog).getByRole("button", { name: "Forget" }));
    await waitFor(() => expect(api.callsTo("DELETE", "/api/v1/workspaces/me/ask/conversations/c-1")).toHaveLength(1));
    await waitFor(() => expect(screen.queryByText("₹10,00,000", { selector: "strong" })).not.toBeInTheDocument());
  });
});

describe("workspace isolation", () => {
  function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((r) => {
      resolve = r;
    });
    return { promise, resolve };
  }

  it("Rahul's answer released after switching to Priya never renders under Priya", async () => {
    const held = deferred<{ status: number; body: unknown }>();
    const rahulAnswer = question({ id: "q-rahul", question: "RAHUL-QUESTION", answer: answer({ blocks: [{ type: "paragraph", parts: [{ text: "RAHUL-ANSWER-MARKER" }] }] }) });
    const api = mockApi({
      ...routesFor(RAHUL, { [`POST /api/v1/workspaces/${RAHUL}/ask`]: () => held.promise }),
      ...routesFor(PRIYA),
      [`GET /api/v1/workspaces/${RAHUL}`]: subject(RAHUL, "Rahul Sharma"),
      [`GET /api/v1/workspaces/${PRIYA}`]: subject(PRIYA, "Priya Patel"),
    });
    const client = createTestQueryClient();
    nav.pathname = `/admin/users/${RAHUL}/ask`;
    const view = renderWithProviders(<AskWorkspaceView />, { viewer: admin, client });
    await askFor("RAHUL-QUESTION");
    await waitFor(() => expect(api.callsTo("POST", `/api/v1/workspaces/${RAHUL}/ask`)).toHaveLength(1));

    nav.pathname = `/admin/users/${PRIYA}/ask`;
    view.rerender(<AskWorkspaceView />);
    expect(await screen.findByText("Answers come only from Priya Patel's records.")).toBeInTheDocument();
    await act(async () => {
      held.resolve({ status: 201, body: rahulAnswer });
      await held.promise;
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("RAHUL-QUESTION")).not.toBeInTheDocument();
    expect(screen.queryByText("RAHUL-ANSWER-MARKER")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Questions and answers" })).not.toBeInTheDocument();
    // Priya's screen only ever asked Priya's endpoints after the switch.
    const afterSwitch = api.calls.filter((c) => c.path.includes(PRIYA));
    expect(afterSwitch.every((c) => !c.path.includes(RAHUL))).toBe(true);
    // Rahul's answer was cached under Rahul's key only.
    const cached = client.getQueryCache().findAll({ queryKey: ["ask", PRIYA] });
    expect(JSON.stringify(cached.map((q) => q.state.data))).not.toContain("RAHUL");
  });

  it("a poll for Rahul's pending question that returns after the switch stays Rahul's", async () => {
    const heldPoll = deferred<{ status: number; body: unknown }>();
    mockApi({
      ...routesFor(RAHUL, {
        [`POST /api/v1/workspaces/${RAHUL}/ask`]: { status: 201, body: question({ id: "q-r", question: "RAHUL-PENDING", status: "pending", answer: null, finished_at: null }) },
        [`GET /api/v1/workspaces/${RAHUL}/ask/questions/q-r`]: () => heldPoll.promise,
      }),
      ...routesFor(PRIYA),
      [`GET /api/v1/workspaces/${RAHUL}`]: subject(RAHUL, "Rahul Sharma"),
      [`GET /api/v1/workspaces/${PRIYA}`]: subject(PRIYA, "Priya Patel"),
    });
    nav.pathname = `/admin/users/${RAHUL}/ask`;
    const view = renderWithProviders(<AskWorkspaceView />, { viewer: admin });
    await askFor("RAHUL-PENDING");
    expect(await screen.findByText("RAHUL-PENDING")).toBeInTheDocument();
    nav.pathname = `/admin/users/${PRIYA}/ask`;
    view.rerender(<AskWorkspaceView />);
    await screen.findByText("Answers come only from Priya Patel's records.");
    await act(async () => {
      heldPoll.resolve({ status: 200, body: question({ id: "q-r", question: "RAHUL-PENDING", answer: answer({ blocks: [{ type: "paragraph", parts: [{ text: "RAHUL-LATE" }] }] }) }) });
      await heldPoll.promise;
    });
    expect(screen.queryByText("RAHUL-PENDING")).not.toBeInTheDocument();
    expect(screen.queryByText("RAHUL-LATE")).not.toBeInTheDocument();
  });
});

describe("navigation", () => {
  it("offers Ask Arkray only when it is on and the viewer may ask, inside the workspace", () => {
    nav.pathname = `/admin/users/${RAHUL}/dashboard`;
    mockApi({ [`GET /api/v1/workspaces/${RAHUL}`]: subject(RAHUL, "Rahul Sharma") });
    const view = renderWithProviders(<Sidebar />, { viewer: admin });
    expect(screen.getByRole("link", { name: "Ask Arkray" })).toHaveAttribute("href", `/admin/users/${RAHUL}/ask`);
    view.unmount();
    renderWithProviders(<Sidebar />, { viewer: { ...admin, features: { ask: false } } });
    expect(screen.queryByRole("link", { name: "Ask Arkray" })).not.toBeInTheDocument();
  });

  it("marks Ask Arkray as the current page", () => {
    nav.pathname = "/ask";
    mockApi({});
    renderWithProviders(<Sidebar />, { viewer: asker });
    expect(screen.getByRole("link", { name: "Ask Arkray" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Dashboard" })).not.toHaveAttribute("aria-current");
  });
});
