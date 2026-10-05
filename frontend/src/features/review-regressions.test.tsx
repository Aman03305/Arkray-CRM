/**
 * Regression tests for the Phase 1 adversarial frontend review (one per confirmed finding
 * not already covered next to its component).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/shell/AppShell";
import { Sidebar } from "@/components/shell/Sidebar";
import { ActivateAccountForm } from "@/features/auth/ActivateAccountForm";
import { LoginForm } from "@/features/auth/LoginForm";
import { UsersPage } from "@/features/users/UsersPage";
import { DashboardView } from "@/features/workspace/views";
import { ApiError, apiFetch, readCookie } from "@/lib/api/client";
import { adminViewer, makeAdminUser } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ search: "", pathname: "/dashboard", hardNavigate: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.search),
  usePathname: () => nav.pathname,
}));
vi.mock("@/lib/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/browser")>()),
  hardNavigate: nav.hardNavigate,
  currentLocation: () => "/",
}));

const TOKEN = "B".repeat(43);
const VIEWER_DTO = {
  id: "u1",
  email: "rahul@example.test",
  first_name: "Rahul",
  last_name: "Sharma",
  full_name: "Rahul Sharma",
  role: "sales_user",
  role_label: "User",
  capabilities: ["crm.access_own"],
};

beforeEach(() => {
  nav.search = "";
  nav.pathname = "/dashboard";
  nav.hardNavigate.mockReset();
});

describe("activation page (review P2-2)", () => {
  it("treats an outage as 'try again', not as a dead invitation", async () => {
    let attempts = 0;
    mockApi({
      "POST /api/v1/auth/invitations/verify": () => {
        attempts += 1;
        return attempts === 1
          ? apiError(503, "service_unavailable", "Down.")
          : { status: 200, body: { email: "neha@example.test", first_name: "Neha" } };
      },
    });
    renderWithProviders(<ActivateAccountForm token={TOKEN} />);
    expect(await screen.findByRole("heading", { name: "We couldn't check your invitation" })).toBeInTheDocument();
    expect(screen.queryByText(/Ask your Arkray CRM administrator/)).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Welcome, Neha" })).toBeInTheDocument();
  });
});

describe("sign-in page", () => {
  it.each(["constructor", "toString", "__proto__"])("ignores reason=%s (no empty alert)", (reason) => {
    mockApi({ "GET /api/v1/auth/me": apiError(401, "not_authenticated", "x") });
    nav.search = `reason=${reason}`;
    renderWithProviders(<LoginForm />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("does not forward a new user into someone else's open session", async () => {
    mockApi({ "GET /api/v1/auth/me": { status: 200, body: VIEWER_DTO } });
    nav.search = "reason=activated";
    renderWithProviders(<LoginForm />);
    expect(await screen.findByText(/still signed in as Rahul Sharma/)).toBeInTheDocument();
    expect(nav.hardNavigate).not.toHaveBeenCalled();
  });
});

describe("edit conflicts (review P2-3)", () => {
  it("a 409 refreshes the list so reopening the dialog uses the latest version", async () => {
    let version = 3;
    const api = mockApi({
      "GET /api/v1/admin/users": () => ({
        status: 200,
        body: { results: [makeAdminUser({ version })], next: null, previous: null },
      }),
      [`PATCH /api/v1/admin/users/${makeAdminUser().id}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 4
          ? { status: 200, body: makeAdminUser({ version: 5, first_name: "R" }) }
          : apiError(409, "conflict", "The record was changed by someone else."),
    });
    renderWithProviders(<UsersPage />, { viewer: adminViewer });
    const user = userEvent.setup();
    const edit = async () => {
      await user.click(await screen.findByRole("button", { name: "Actions for Rahul Sharma" }));
      await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
      await user.selectOptions(screen.getByLabelText("Role"), "admin");
      await user.click(screen.getByRole("button", { name: "Save changes" }));
    };
    version = 4; // someone else saved meanwhile
    await edit();
    expect(await screen.findByText(/reopen it to edit the latest details/)).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/admin/users").length).toBeGreaterThan(1));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await edit();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const sent = api.callsTo("PATCH", `/api/v1/admin/users/${makeAdminUser().id}`).map((c) => (c.body as { version: number }).version);
    expect(sent).toEqual([3, 4]);
  });
});

describe("changing your own email", () => {
  it("asks for your current password", async () => {
    const self = makeAdminUser({ id: adminViewer.id, full_name: "Anita Admin", first_name: "Anita", last_name: "Admin" });
    const api = mockApi({
      "GET /api/v1/admin/users": { status: 200, body: { results: [self], next: null, previous: null } },
      [`POST /api/v1/admin/users/${self.id}/change-email`]: { status: 200, body: { ...self, email: "anita@new.test" } },
    });
    renderWithProviders(<UsersPage />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Anita Admin" }));
    await user.click(screen.getByRole("menuitem", { name: "Change email" }));
    const dialog = screen.getByRole("dialog");
    await user.clear(within(dialog).getByLabelText("New email"));
    await user.type(within(dialog).getByLabelText("New email"), "anita@new.test");
    await user.click(within(dialog).getByRole("button", { name: "Change email" }));
    expect(within(dialog).getByLabelText("Your current password")).toHaveFocus();
    expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    await user.type(within(dialog).getByLabelText("Your current password"), "my-password-1");
    await user.click(within(dialog).getByRole("button", { name: "Change email" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.callsTo("POST", `/api/v1/admin/users/${self.id}/change-email`)[0]!.body).toMatchObject({
      current_password: "my-password-1",
    });
  });
});

describe("focus management (review P2-4, P2-5)", () => {
  it("the mobile drawer takes focus, traps Tab and returns focus to the menu button", async () => {
    mockApi({});
    renderWithProviders(<AppShell>Page</AppShell>, { viewer: adminViewer });
    const user = userEvent.setup();
    const menuButton = screen.getByRole("button", { name: "Open navigation" });
    await user.click(menuButton);
    const drawer = screen.getByRole("dialog", { name: "Navigation" });
    expect(drawer).toContainElement(document.activeElement as HTMLElement);
    for (let i = 0; i < 15; i++) {
      await user.tab();
      expect(drawer).toContainElement(document.activeElement as HTMLElement);
    }
    await user.keyboard("{Escape}");
    expect(menuButton).toHaveFocus();
  });

  it("the row menu renders outside the scrolling table (review P3-1)", async () => {
    mockApi({ "GET /api/v1/admin/users": { status: 200, body: { results: [makeAdminUser()], next: null, previous: null } } });
    renderWithProviders(<UsersPage />, { viewer: adminViewer });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Actions for Rahul Sharma" }));
    const menu = screen.getByRole("menu");
    expect(menu.closest(".overflow-x-auto")).toBeNull();
    expect(menu).toHaveClass("fixed");
  });
});

describe("API client (review P3-2, P3-9)", () => {
  it("a malformed CSRF cookie is passed through, not reported as a network failure", async () => {
    expect(readCookie("arkray_csrftoken", "arkray_csrftoken=%E0%A4%A")).toBe("%E0%A4%A");
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    document.cookie = "arkray_csrftoken=%E0%A4%A";
    await expect(apiFetch("/api/v1/auth/logout", { method: "POST" })).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalled();
  });

  it("a timed-out write says the change may already be saved", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
          }),
      ),
    );
    document.cookie = "arkray_csrftoken=t";
    const error = (await apiFetch("/api/v1/admin/users", { method: "POST", body: {}, timeoutMs: 20 }).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error.code).toBe("timeout");
    expect(error.message).toMatch(/check whether the change was saved/);
  });
});

describe("capability-consistent views (review P3-5, P3-7)", () => {
  it("does not guess a workspace (or call the API) before the viewer is known", () => {
    const api = mockApi({});
    renderWithProviders(<DashboardView />);
    expect(screen.getByText("Loading")).toBeInTheDocument();
    expect(screen.queryByText("Key figures")).not.toBeInTheDocument();
    expect(api.calls).toHaveLength(0);
  });
});

describe("sidebar sign-out failure (review P3-4)", () => {
  it("is announced, not hidden in a tooltip", async () => {
    mockApi({ "POST /api/v1/auth/logout": apiError(503, "service_unavailable", "Down.") });
    renderWithProviders(<Sidebar />, { viewer: adminViewer });
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-out failed");
    expect(nav.hardNavigate).not.toHaveBeenCalled();
  });
});
