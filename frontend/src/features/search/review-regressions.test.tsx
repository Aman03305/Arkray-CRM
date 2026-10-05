/**
 * Phase 7 frontend review: one regression test per confirmed finding (docs/search.md).
 * Each failed on the code before its fix.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Dialog } from "@/components/ui/Dialog";
import type { SearchResults } from "@/lib/api/types";
import { RAHUL_ID, adminViewer, salesViewer } from "@/test/fixtures";
import { mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

import { queryState } from "./query";
import { SearchLauncher } from "./SearchLauncher";

const nav = vi.hoisted(() => ({ pathname: "/dashboard", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
}));

beforeEach(() => {
  nav.pathname = "/dashboard";
  nav.push.mockReset();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const empty = { results: [], has_more: false };

/** One opportunity named after the query, with an id derived from it. */
function dealFor(q: string): SearchResults {
  const id = q.toLowerCase().startsWith("apollo")
    ? "aaaaaaaa-aaaa-4aaa-8aaa-000000000001"
    : "bbbbbbbb-bbbb-4bbb-8bbb-000000000002";
  return {
    query: q,
    terms: [q],
    leads: empty,
    opportunities: {
      has_more: false,
      results: [
        {
          id,
          title: `${q} Deal`,
          status: "open",
          stage: { id: "s1", name: "Proposal" },
          account_name: `${q} Account`,
          customer_name: `${q} Customer`,
          lead: { id: "cccccccc-cccc-4ccc-8ccc-000000000003", display_name: `${q} Customer`, organization_name: "", restricted: false },
          owner: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true },
        },
      ],
    },
    tasks: empty,
    meetings: empty,
    notes: empty,
  };
}

function holdable() {
  const gates = new Map<string, () => void>();
  const hold = new Set<string>();
  const api = mockApi({
    "GET /api/v1/workspaces/me/search": async (call: RecordedCall) => {
      const q = call.query.get("q")!;
      if (hold.has(q)) await new Promise<void>((resolve) => gates.set(q, resolve));
      return { status: 200, body: dealFor(q) };
    },
  });
  /** Let the held request for `q` answer, once it has been sent. */
  async function release(q: string) {
    await waitFor(() => expect(gates.has(q)).toBe(true));
    gates.get(q)!();
  }
  return { api, hold, release };
}

async function openDialog(user: ReturnType<typeof userEvent.setup>, viewer = salesViewer) {
  renderWithProviders(<SearchLauncher />, { viewer });
  await user.click(screen.getByRole("button", { name: /search/i }));
  return screen.getByRole("combobox");
}

describe("F1: Enter opens a result of what is in the box, never of the previous query", () => {
  it("Enter right after retyping waits for the new results", async () => {
    const { hold, release } = holdable();
    const user = userEvent.setup();
    const input = await openDialog(user);
    await user.type(input, "Apollo");
    await screen.findByRole("option", { name: /^Opportunity: Apollo Deal/ });
    hold.add("Zeta");
    await user.keyboard("{Control>}a{/Control}Zeta{Enter}");
    expect(nav.push).not.toHaveBeenCalled(); // not Apollo's deal
    await release("Zeta");
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/pipeline/bbbbbbbb-bbbb-4bbb-8bbb-000000000002"));
    expect(nav.push).toHaveBeenCalledTimes(1);
  });

  it("an unrelated query never shows the previous query's results while it loads", async () => {
    const { hold } = holdable();
    const user = userEvent.setup();
    const input = await openDialog(user);
    await user.type(input, "Apollo");
    await screen.findByRole("option", { name: /^Opportunity: Apollo Deal/ });
    hold.add("Zeta");
    await user.clear(input);
    await user.type(input, "Zeta");
    await screen.findByText("Searching…", { selector: "p:not([role])" });
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("a longer version of the same query keeps the results on screen, but Enter still waits", async () => {
    const { hold, release } = holdable();
    const user = userEvent.setup();
    const input = await openDialog(user);
    await user.type(input, "Apollo");
    await screen.findByRole("option", { name: /^Opportunity: Apollo Deal/ });
    hold.add("Apollo Labs");
    await user.type(input, " Labs");
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect(screen.getByRole("option", { name: /^Opportunity: Apollo Deal/ })).toBeInTheDocument(); // dimmed
    await user.keyboard("{Enter}");
    expect(nav.push).not.toHaveBeenCalled();
    await release("Apollo Labs");
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/pipeline/aaaaaaaa-aaaa-4aaa-8aaa-000000000001"));
  });
});

describe("F2: focus never leaves the dialog", () => {
  it("Tab and Shift+Tab cycle between the box and Close once results are shown", async () => {
    holdable();
    const user = userEvent.setup();
    const input = await openDialog(user);
    await user.type(input, "Apollo");
    await screen.findByRole("option");
    const close = screen.getByRole("button", { name: "Close dialog" });
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab();
    expect(input).toHaveFocus();
    await user.tab({ shift: true });
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(input).toHaveFocus();
    expect(screen.getByRole("listbox")).toHaveAttribute("tabindex", "-1");
  });

  it("the shared trap skips elements taken out of the tab order in every dialog", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Dialog open title="Check" onClose={() => {}}>
        <input aria-label="Field" />
        <a href="/x" tabIndex={-1}>
          Not tabbable
        </a>
      </Dialog>,
    );
    const field = screen.getByRole("textbox", { name: "Field" });
    field.focus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Close dialog" })).toHaveFocus();
  });
});

describe("F3: every finished search is announced", () => {
  it("the status says Searching while results update, then the count again", async () => {
    const { hold, release } = holdable();
    const user = userEvent.setup();
    const input = await openDialog(user);
    await user.type(input, "Apollo");
    await screen.findByRole("option");
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("1 result");
    hold.add("Apollo Labs");
    await user.type(input, " Labs");
    await waitFor(() => expect(status).toHaveTextContent("Searching…"));
    await release("Apollo Labs");
    await waitFor(() => expect(status).toHaveTextContent("1 result"));
  });
});

describe("F4: no guessed workspace", () => {
  it("search is disabled until the viewer is known, and never asks for one's own records instead", () => {
    const api = mockApi({});
    renderWithProviders(<SearchLauncher />, { viewer: null });
    expect(screen.getByRole("button", { name: /search/i })).toBeDisabled();
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.calls).toHaveLength(0);
  });

  it("a user's workspace the viewer may not open has no search", () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    mockApi({});
    renderWithProviders(<SearchLauncher />, { viewer: salesViewer });
    expect(screen.getByRole("button", { name: /search/i })).toBeDisabled();
  });

  it("an administrator's selected workspace does", () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    mockApi({});
    renderWithProviders(<SearchLauncher />, { viewer: adminViewer });
    expect(screen.getByRole("button", { name: /search/i })).toBeEnabled();
  });
});

describe("F5: the box follows the API's rules", () => {
  const cp = String.fromCodePoint;
  it.each([
    [`Rahul${cp(9)}Sharma`, "ready", "Rahul Sharma"], // a pasted tab
    [`Rahul${cp(10)}Sharma`, "ready", "Rahul Sharma"],
    [`Rahul${cp(0xa0)}Sharma`, "ready", "Rahul Sharma"], // no-break space
    [`ab${cp(0x3000)}cd`, "idle", ""], // ideographic space splits: two 2-letter words
    [`田中${cp(0x3000)}太郎さん`, "ready", `田中 太郎さん`],
    [`abc${cp(0xfdd0)}`, "invalid", ""], // noncharacter
    [`abc${cp(0x1fffe)}`, "invalid", ""],
    [`abc${cp(0x0b)}`, "invalid", ""], // vertical tab
  ])("%j is %s", (raw, kind, query) => {
    const state = queryState(raw);
    expect(state.kind).toBe(kind);
    if (state.kind === "ready") expect(state.query).toBe(query);
  });
});

describe("F6-F9 and input methods", () => {
  it("F6: a long name in the dialog header can wrap", () => {
    renderWithProviders(
      <Dialog open title="Search" description={`Searching ${"x".repeat(60)}'s records.`} onClose={() => {}}>
        <p>Body</p>
      </Dialog>,
    );
    expect(screen.getByRole("heading", { name: "Search" }).parentElement).toHaveClass("min-w-0");
  });

  it("F7: options keep clear of the sticky group headings when scrolled to", async () => {
    holdable();
    const user = userEvent.setup();
    await user.type(await openDialog(user), "Apollo");
    expect(await screen.findByRole("option")).toHaveClass("scroll-mt-9");
  });

  it("F8: an invalid query is exposed and announced", async () => {
    const api = mockApi({});
    const user = userEvent.setup();
    const input = await openDialog(user);
    fireEvent.change(input, { target: { value: `Rahul${String.fromCodePoint(0x200b)}` } });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("status")).toHaveTextContent("Remove the invisible or control characters");
    expect(api.calls).toHaveLength(0);
  });

  it("F9: Ctrl+K works on a non-Latin keyboard layout", () => {
    mockApi({});
    renderWithProviders(<SearchLauncher />, { viewer: salesViewer });
    fireEvent.keyDown(document.body, { key: "л", code: "KeyK", ctrlKey: true });
    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
  });

  it("text still being composed with an input method isn't searched, and Escape cancels the composition only", async () => {
    const { api } = holdable();
    const user = userEvent.setup();
    const input = await openDialog(user);
    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "らふる" } });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(api.calls).toHaveLength(0);
    fireEvent.keyDown(input, { key: "Escape", isComposing: true });
    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
    fireEvent.compositionEnd(input);
    await waitFor(() => expect(api.calls).toHaveLength(1));
    expect(api.calls[0]!.query.get("q")).toBe("らふる");
  });

  it("Enter that confirms an input method's candidate (Safari: keyCode 229) opens nothing", async () => {
    holdable();
    const user = userEvent.setup();
    const input = await openDialog(user);
    await user.type(input, "Apollo");
    await screen.findByRole("option");
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(nav.push).not.toHaveBeenCalled();
  });
});
