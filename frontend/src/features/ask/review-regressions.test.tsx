/**
 * Phase 8 frontend review regressions for Ask Arkray: polling always ends, conversations
 * never mix, failures are explained, and the small UX/a11y defects stay fixed. Each test
 * failed on the code the review examined.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/shell/Sidebar";
import { AskWorkspaceView } from "@/features/workspace/views";
import type { AskAnswer, AskQuestion } from "@/lib/api/types";
import { adminViewer, makeViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/ask" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
}));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  nav.pathname = "/ask";
});

const RAHUL = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";
const asker = makeViewer({ features: { ask: true } });
const NOTE = "22222222-2222-4222-8222-222222222222";

function answer(text: string, overrides: Partial<AskAnswer> = {}): AskAnswer {
  return {
    blocks: [{ type: "paragraph", parts: [{ text }] }],
    facts: [],
    sources: [],
    citations: [],
    notices: [],
    provenance: { mode: "router", tools: [], grounded: true, model: "" },
    ...overrides,
  };
}

function question(id: string, overrides: Partial<AskQuestion> = {}): AskQuestion {
  return {
    id,
    conversation_id: `c-${id}`,
    question: `Q ${id}`,
    status: "answered",
    error: null,
    answer: answer(`A ${id}`),
    created_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    ...overrides,
  };
}

const pending = (id: string, overrides: Partial<AskQuestion> = {}) =>
  question(id, { status: "pending", answer: null, finished_at: null, ...overrides });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const BASE = {
  "GET /api/v1/workspaces/me/ask": { status: 200, body: { enabled: true, summaries: "available" } },
  "GET /api/v1/workspaces/me/ask/conversations": { status: 200, body: [] },
};

async function type(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByRole("textbox"), text);
  return user;
}

describe("polling always ends", () => {
  it("keeps polling when the server's clock is far behind the browser's (limit measured from first sight)", async () => {
    const skewed = new Date(Date.now() - 3 * 60_000).toISOString(); // server 3 min behind
    let polls = 0;
    mockApi({
      ...BASE,
      "POST /api/v1/workspaces/me/ask": { status: 201, body: pending("q1", { created_at: skewed }) },
      "GET /api/v1/workspaces/me/ask/questions/q1": () => {
        polls += 1;
        return { status: 200, body: polls < 2 ? pending("q1", { created_at: skewed }) : question("q1") };
      },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = await type("Why?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("A q1", {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it("a question that is gone (404) fails visibly and frees the Ask button", async () => {
    mockApi({
      ...BASE,
      "POST /api/v1/workspaces/me/ask": { status: 201, body: pending("q1") },
      "GET /api/v1/workspaces/me/ask/questions/q1": apiError(404, "not_found", "Not found."),
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = await type("Why?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByRole("alert", {}, { timeout: 5000 })).toHaveTextContent("didn't arrive");
    await user.type(screen.getByRole("textbox"), "Again");
    expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled();
  });

  it("gives up after the poll limit when the server keeps failing", async () => {
    mockApi({
      ...BASE,
      "POST /api/v1/workspaces/me/ask": { status: 201, body: pending("q1") },
      "GET /api/v1/workspaces/me/ask/questions/q1": apiError(502, "bad_gateway", "Bad gateway."),
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = await type("Why?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Q q1");
    const later = Date.now() + 3 * 60_000; // well past the poll limit, by this browser's clock
    vi.spyOn(Date, "now").mockReturnValue(later);
    expect(await screen.findByRole("alert", {}, { timeout: 5000 })).toHaveTextContent("didn't arrive");
  });
});

describe("conversations never mix", () => {
  it("opening B while A's question is in flight shows only B, and the next question goes to B", async () => {
    const heldAsk = deferred<{ status: number; body: unknown }>();
    let posts = 0;
    const api = mockApi({
      ...BASE,
      "GET /api/v1/workspaces/me/ask/conversations": { status: 200, body: [{ id: "c-b", title: "B-QUESTION", created_at: "", updated_at: "" }] },
      "GET /api/v1/workspaces/me/ask/conversations/c-b": {
        status: 200,
        body: { id: "c-b", created_at: "", updated_at: "", questions: [question("b", { question: "B-QUESTION", conversation_id: "c-b" })] },
      },
      "POST /api/v1/workspaces/me/ask": () => {
        posts += 1;
        return posts === 1 ? heldAsk.promise : { status: 201, body: question("f", { question: "FOLLOW", conversation_id: "c-b" }) };
      },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = await type("NEW-QUESTION");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await user.click(await screen.findByRole("button", { name: "B-QUESTION" }));
    await screen.findByText("A b");
    await act(async () => {
      heldAsk.resolve({ status: 201, body: question("n", { question: "NEW-QUESTION", conversation_id: "c-new" }) });
      await heldAsk.promise;
    });
    await new Promise((r) => setTimeout(r, 50));
    const thread = screen.getByRole("list", { name: "Questions and answers" });
    expect(within(thread).queryByText("NEW-QUESTION")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "B-QUESTION" })).toHaveAttribute("aria-current", "true");
    await user.type(screen.getByRole("textbox"), "FOLLOW{Enter}");
    await screen.findByText("A f");
    const posted = api.callsTo("POST", "/api/v1/workspaces/me/ask").map((c) => (c.body as { conversation_id?: string }).conversation_id ?? null);
    expect(posted).toEqual([null, "c-b"]);
  });

  it("the slower of two opens doesn't replace the later click", async () => {
    const slowA = deferred<{ status: number; body: unknown }>();
    mockApi({
      ...BASE,
      "GET /api/v1/workspaces/me/ask/conversations": {
        status: 200,
        body: [
          { id: "c-a", title: "OPEN-A", created_at: "", updated_at: "" },
          { id: "c-b", title: "OPEN-B", created_at: "", updated_at: "" },
        ],
      },
      "GET /api/v1/workspaces/me/ask/conversations/c-a": () => slowA.promise,
      "GET /api/v1/workspaces/me/ask/conversations/c-b": {
        status: 200,
        body: { id: "c-b", created_at: "", updated_at: "", questions: [question("b", { conversation_id: "c-b" })] },
      },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "OPEN-A" }));
    await user.click(screen.getByRole("button", { name: "OPEN-B" }));
    await screen.findByText("A b");
    await act(async () => {
      slowA.resolve({ status: 200, body: { id: "c-a", created_at: "", updated_at: "", questions: [question("a", { conversation_id: "c-a" })] } });
      await slowA.promise;
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("A a")).not.toBeInTheDocument();
    expect(screen.getByText("A b")).toBeInTheDocument();
  });

  it("a follow-up in a conversation forgotten elsewhere says so, and the next question starts a new one", async () => {
    const api = mockApi({
      ...BASE,
      "GET /api/v1/workspaces/me/ask/conversations": { status: 200, body: [{ id: "c-1", title: "OLD", created_at: "", updated_at: "" }] },
      "GET /api/v1/workspaces/me/ask/conversations/c-1": {
        status: 200,
        body: { id: "c-1", created_at: "", updated_at: "", questions: [question("1", { conversation_id: "c-1" })] },
      },
      "POST /api/v1/workspaces/me/ask": (call) =>
        (call.body as { conversation_id?: string }).conversation_id
          ? apiError(404, "not_found", "Not found.")
          : { status: 201, body: question("2") },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "OLD" }));
    await screen.findByText("A 1");
    await user.type(screen.getByRole("textbox"), "Follow-up");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/That conversation no longer exists/)).toBeInTheDocument();
    await user.type(screen.getByRole("textbox"), "{Enter}");
    await screen.findByText("A 2");
    const posted = api.callsTo("POST", "/api/v1/workspaces/me/ask").map((c) => (c.body as { conversation_id?: string }).conversation_id ?? null);
    expect(posted).toEqual(["c-1", null]);
  });

  it("reopening a conversation shows the follow-ups asked in it", async () => {
    let version = 1;
    mockApi({
      ...BASE,
      "GET /api/v1/workspaces/me/ask/conversations": { status: 200, body: [{ id: "c-1", title: "CONV", created_at: "", updated_at: "" }] },
      "GET /api/v1/workspaces/me/ask/conversations/c-1": () => ({
        status: 200,
        body: {
          id: "c-1",
          created_at: "",
          updated_at: "",
          questions: version === 1 ? [question("1", { conversation_id: "c-1" })] : [question("1", { conversation_id: "c-1" }), question("2", { conversation_id: "c-1" })],
        },
      }),
      "POST /api/v1/workspaces/me/ask": () => {
        version = 2;
        return { status: 201, body: question("2", { conversation_id: "c-1" }) };
      },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "CONV" }));
    await screen.findByText("A 1");
    await user.type(screen.getByRole("textbox"), "More{Enter}");
    await screen.findByText("A 2");
    await user.click(screen.getByRole("button", { name: "New conversation" }));
    await user.click(screen.getByRole("button", { name: "CONV" }));
    expect(await screen.findByText("A 2")).toBeInTheDocument();
  });
});

describe("forgetting", () => {
  async function openConversation(routes: Record<string, unknown>) {
    const api = mockApi({
      ...BASE,
      "GET /api/v1/workspaces/me/ask/conversations": { status: 200, body: [{ id: "c-1", title: "CONV", created_at: "", updated_at: "" }] },
      "GET /api/v1/workspaces/me/ask/conversations/c-1": {
        status: 200,
        body: { id: "c-1", created_at: "", updated_at: "", questions: [question("1", { conversation_id: "c-1" })] },
      },
      ...(routes as Parameters<typeof mockApi>[0]),
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "CONV" }));
    await screen.findByText("A 1");
    return { api, user };
  }

  it("asks for confirmation first", async () => {
    const { api, user } = await openConversation({ "DELETE /api/v1/workspaces/me/ask/conversations/c-1": { status: 204 } });
    await user.click(screen.getByRole("button", { name: "Forget" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Forget this conversation?" });
    expect(api.callsTo("DELETE", "/api/v1/workspaces/me/ask/conversations/c-1")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("A 1")).toBeInTheDocument();
  });

  it("a failed deletion is shown, not silent", async () => {
    const { user } = await openConversation({
      "DELETE /api/v1/workspaces/me/ask/conversations/c-1": apiError(503, "service_unavailable", "Try again later."),
    });
    await user.click(screen.getByRole("button", { name: "Forget" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Forget" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Try again later.");
    expect(screen.getByText("A 1")).toBeInTheDocument();
  });

  it("an already-forgotten conversation (404) counts as forgotten", async () => {
    const { user } = await openConversation({
      "DELETE /api/v1/workspaces/me/ask/conversations/c-1": apiError(404, "not_found", "Not found."),
    });
    await user.click(screen.getByRole("button", { name: "Forget" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Forget" }));
    await waitFor(() => expect(screen.queryByText("A 1")).not.toBeInTheDocument());
  });
});

describe("input and feedback", () => {
  it("Enter while a question is pending doesn't send another", async () => {
    const api = mockApi({
      ...BASE,
      "POST /api/v1/workspaces/me/ask": { status: 201, body: pending("q1") },
      "GET /api/v1/workspaces/me/ask/questions/q1": { status: 200, body: pending("q1") },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = await type("First{Enter}");
    await screen.findByText("Q q1");
    await user.type(screen.getByRole("textbox"), "Second{Enter}");
    expect(api.callsTo("POST", "/api/v1/workspaces/me/ask")).toHaveLength(1);
  });

  it("the Enter ending an IME composition (keyCode 229) doesn't send", async () => {
    const api = mockApi({ ...BASE, "POST /api/v1/workspaces/me/ask": { status: 201, body: question("q1") } });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await type("日本");
    const box = screen.getByRole("textbox");
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 229, bubbles: true }));
    await new Promise((r) => setTimeout(r, 30));
    expect(api.callsTo("POST", "/api/v1/workspaces/me/ask")).toHaveLength(0);
  });

  it("explains why a question was refused", async () => {
    mockApi({
      ...BASE,
      "POST /api/v1/workspaces/me/ask": apiError(400, "validation_error", "Some fields are invalid.", {
        question: ["Remove the invisible or control characters from this text."],
      }),
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await type("Hidden{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Remove the invisible or control characters");
  });

  it("a suggestion keeps what was typed, and focus returns to the box", async () => {
    mockApi({ ...BASE, "POST /api/v1/workspaces/me/ask": { status: 201, body: question("q1") } });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    const user = await type("my draft");
    await user.click(screen.getByRole("button", { name: "What is the pipeline value?" }));
    await screen.findByText("A q1");
    expect(screen.getByRole("textbox")).toHaveValue("my draft");
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "New conversation" }));
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveFocus());
  });

  it("a failed conversation list can be retried", async () => {
    let calls = 0;
    mockApi({
      ...BASE,
      "GET /api/v1/workspaces/me/ask/conversations": () => {
        calls += 1;
        return calls === 1 ? apiError(500, "server_error", "Oops") : { status: 200, body: [{ id: "c-1", title: "BACK", created_at: "", updated_at: "" }] };
      },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "BACK" })).toBeInTheDocument();
  });

  it("opening a conversation shows that it is loading", async () => {
    const slow = deferred<{ status: number; body: unknown }>();
    mockApi({
      ...BASE,
      "GET /api/v1/workspaces/me/ask/conversations": { status: 200, body: [{ id: "c-1", title: "SLOW", created_at: "", updated_at: "" }] },
      "GET /api/v1/workspaces/me/ask/conversations/c-1": () => slow.promise,
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await userEvent.setup().click(await screen.findByRole("button", { name: "SLOW" }));
    expect(await screen.findByText("Opening the conversation…")).toBeInTheDocument();
    await act(async () => {
      slow.resolve({ status: 200, body: { id: "c-1", created_at: "", updated_at: "", questions: [] } });
      await slow.promise;
    });
  });

  it("the same record quoted twice renders without a key collision", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    mockApi({
      ...BASE,
      "POST /api/v1/workspaces/me/ask": {
        status: 201,
        body: question("q1", {
          answer: answer("Quotes", {
            citations: [
              { ref: `note:${NOTE}`, kind: "note", label: "Note", snippet: "first part", when: "" },
              { ref: `note:${NOTE}`, kind: "note", label: "Note", snippet: "second part", when: "" },
            ],
          }),
        }),
      },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: asker });
    await type("Quote{Enter}");
    await screen.findByText("second part");
    expect(errors.mock.calls.map((c) => String(c[0])).join(" ")).not.toMatch(/same key/);
  });
});

it("only Ask Arkray is the current page on a user's Ask page", () => {
  nav.pathname = `/admin/users/${RAHUL}/ask`;
  mockApi({ [`GET /api/v1/workspaces/${RAHUL}`]: { status: 200, body: { subject: { id: RAHUL, full_name: "Rahul Sharma", status: "active", is_active: true } } } });
  renderWithProviders(<Sidebar />, { viewer: { ...adminViewer, features: { ask: true } } });
  const current = screen.getAllByRole("link").filter((link) => link.getAttribute("aria-current") === "page");
  expect(current.map((link) => link.textContent)).toEqual(["Ask Arkray"]);
});
