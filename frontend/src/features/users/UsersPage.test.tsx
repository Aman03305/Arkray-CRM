import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AdminUser } from "@/lib/api/types";
import { setFlash } from "@/lib/flash";
import { formatDate } from "@/lib/format";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import type { Viewer } from "@/lib/viewer";
import { adminViewer, makeAdminUser } from "@/test/fixtures";
import { apiError, createTestQueryClient, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { UsersPage } from "./UsersPage";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/users",
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

beforeEach(() => {
  nav.push.mockReset();
  nav.replace.mockReset();
});

const LIST = "GET /api/v1/admin/users";
const rahul = makeAdminUser();
const invited = makeAdminUser({
  id: "7c6f7f1e-1111-4222-8333-444455556666",
  email: "neha@example.test",
  first_name: "Neha",
  last_name: "Verma",
  full_name: "Neha Verma",
  status: "invited",
  status_label: "Invited",
  last_login: null,
  activated_at: null,
  invitation: { expires_at: "2099-01-01T00:00:00Z", sent_at: "2026-09-30T10:00:00Z", expired: false },
});
const self = makeAdminUser({
  id: adminViewer.id,
  email: adminViewer.email,
  first_name: "Anita",
  last_name: "Admin",
  full_name: "Anita Admin",
  role: "admin",
  role_label: "Admin",
});

function page(results: AdminUser[], next: string | null = null, previous: string | null = null) {
  return { status: 200, body: { results, next, previous } };
}

function renderPage() {
  return renderWithProviders(<UsersPage />, { viewer: adminViewer });
}

async function openActions(name: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: `Actions for ${name}` }));
  return user;
}

describe("UsersPage: states", () => {
  it("shows a loading table, then the users with their details", async () => {
    mockApi({ [LIST]: page([rahul, invited, self]) });
    renderPage();
    expect(screen.getByRole("table", { name: /users \(loading\)/i })).toHaveAttribute("aria-busy", "true");

    const row = (await screen.findByRole("button", { name: "Rahul Sharma" })).closest("tr")!;
    expect(within(row).getByText("rahul@example.test")).toBeInTheDocument();
    expect(within(row).getByText("User")).toBeInTheDocument();
    expect(within(row).getByText("Active")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "Open CRM for Rahul Sharma" })).toHaveAttribute(
      "href",
      `/admin/users/${rahul.id}/dashboard`,
    );
    const invitedRow = screen.getByRole("button", { name: "Neha Verma" }).closest("tr")!;
    expect(within(invitedRow).getByText("Invited")).toBeInTheDocument();
    expect(within(invitedRow).getByText(/Link expires/)).toBeInTheDocument();
    expect(within(invitedRow).getByText("Never")).toBeInTheDocument();
    expect(screen.getByText("(you)")).toBeInTheDocument();
  });

  it("shows the table columns the brief asks for, and no invented CRM metrics", async () => {
    mockApi({ [LIST]: page([rahul]) });
    renderPage();
    await screen.findByRole("button", { name: "Rahul Sharma" });
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Name", "Email", "Role", "Status", "Last login", "Created", "Actions"]);
    expect(screen.queryByText(/pipeline value|active leads/i)).not.toBeInTheDocument();
  });

  it("has an empty state", async () => {
    mockApi({ [LIST]: page([]) });
    renderPage();
    expect(await screen.findByText("No users yet")).toBeInTheDocument();
  });

  it("offers to clear filters when nothing matches", async () => {
    const api = mockApi({ [LIST]: (call: RecordedCall) => page(call.query.get("status") ? [] : [rahul]) });
    renderPage();
    await screen.findByRole("button", { name: "Rahul Sharma" });
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Filter by status"), "deactivated");
    expect(await screen.findByText("No users match your filters")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("button", { name: "Rahul Sharma" })).toBeInTheDocument();
    expect(api.calls.at(-1)!.query.get("status")).toBeNull();
  });

  it("shows server errors with a retry and a reference", async () => {
    let attempts = 0;
    mockApi({
      [LIST]: () => {
        attempts += 1;
        return attempts === 1 ? apiError(500, "server_error", "Boom") : page([rahul]);
      },
    });
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Users couldn't be loaded");
    expect(alert).toHaveTextContent("Reference: req-test-1");
    expect(alert).not.toHaveTextContent("Boom");
    await userEvent.setup().click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Rahul Sharma" })).toBeInTheDocument();
  });

  it("explains an authorization error", async () => {
    mockApi({ [LIST]: apiError(403, "permission_denied", "You do not have permission to perform this action.") });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("You can't manage users");
  });
});

describe("UsersPage: search, filters and pagination", () => {
  it("sends allowlisted filters, debounces search and ignores 1-character searches", async () => {
    const api = mockApi({ [LIST]: page([rahul]) });
    renderPage();
    await screen.findByRole("button", { name: "Rahul Sharma" });
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Search users"), "r");
    expect(screen.getByText("Type at least 2 characters to search.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Search users"), "ahul");
    await user.selectOptions(screen.getByLabelText("Filter by role"), "admin");
    await waitFor(() => expect(api.calls.at(-1)!.query.get("q")).toBe("rahul"));
    const last = api.calls.at(-1)!;
    expect(last.query.get("role")).toBe("admin");
    expect([...last.query.keys()].sort()).toEqual(["page_size", "q", "role"]);
    expect(api.calls.some((c) => c.query.get("q") === "r")).toBe(false);
  });

  it("follows the API's cursors without calling another origin", async () => {
    const api = mockApi({
      [LIST]: (call: RecordedCall) =>
        call.query.get("cursor") === "page2"
          ? page([invited], null, "http://backend:8000/api/v1/admin/users?cursor=page1")
          : page([rahul], "http://backend:8000/api/v1/admin/users?cursor=page2"),
    });
    renderPage();
    await screen.findByRole("button", { name: "Rahul Sharma" });
    const user = userEvent.setup();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("button", { name: "Neha Verma" })).toBeInTheDocument();
    expect(api.fetchMock.mock.calls.every(([url]) => String(url).startsWith("/api/"))).toBe(true);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });
});

describe("UsersPage: creating a user", () => {
  const PASSWORD = "harbour-lantern-91";
  const neha = makeAdminUser({
    id: "7c6f7f1e-2222-4222-8333-444455556666",
    email: "new@example.test",
    first_name: "Neha",
    last_name: "Verma",
    full_name: "Neha Verma",
    role: "sales_user",
    role_label: "Sales user",
    last_login: null,
    password_change_required: true,
  });

  async function fillCreateForm({ email = "new@example.test", password = PASSWORD as string | null } = {}) {
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New user" }));
    const dialog = screen.getByRole("dialog", { name: "New user" });
    await user.type(within(dialog).getByLabelText("First name"), "Neha");
    await user.type(within(dialog).getByLabelText(/Last name/), "Verma");
    await user.type(within(dialog).getByLabelText("Email"), email);
    if (password === null) {
      await user.click(within(dialog).getByRole("button", { name: "Email an invitation instead" }));
      await user.click(within(dialog).getByRole("button", { name: "Create and invite" }));
    } else {
      await user.type(within(dialog).getByLabelText("Initial password"), password);
      await user.click(within(dialog).getByRole("button", { name: "Create user" }));
    }
    return { dialog, user };
  }

  it("an administrator is always invited: no password can be set for one", async () => {
    const api = mockApi({ [LIST]: page([rahul]), "POST /api/v1/admin/users": { status: 201, body: { ...neha, role: "admin" } } });
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New user" }));
    const dialog = screen.getByRole("dialog", { name: "New user" });
    await user.type(within(dialog).getByLabelText("First name"), "Neha");
    await user.type(within(dialog).getByLabelText("Email"), "new@example.test");
    expect(within(dialog).getByLabelText("Initial password")).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Role"), "admin");
    expect(within(dialog).queryByLabelText("Initial password")).not.toBeInTheDocument();
    expect(within(dialog).getByText(/Administrators are invited/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create and invite" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/admin/users")[0]!.body).not.toHaveProperty("password");
  });

  it("creates a user who can sign in at once with the initial password, which is then forgotten", async () => {
    const api = mockApi({ [LIST]: page([rahul]), "POST /api/v1/admin/users": { status: 201, body: neha } });
    renderPage();
    const { user } = await fillCreateForm();
    expect(
      await screen.findByText("Neha Verma can sign in now. They'll choose their own password at first sign-in."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/admin/users")[0]!.body).toEqual({
      first_name: "Neha",
      last_name: "Verma",
      email: "new@example.test",
      role: "sales_user",
      password: PASSWORD,
    });
    expect(document.body.innerHTML).not.toContain(PASSWORD);
    // Nothing of it survives into the next user's form either.
    await user.click(screen.getByRole("button", { name: "New user" }));
    expect(within(screen.getByRole("dialog")).getByLabelText("Initial password")).toHaveValue("");
  });

  it("asks for an initial password by default, hidden unless shown, and can generate a strong one", async () => {
    mockApi({ [LIST]: page([]) });
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New user" }));
    const dialog = screen.getByRole("dialog", { name: "New user" });
    const field = within(dialog).getByLabelText("Initial password");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "off");
    await user.click(within(dialog).getByRole("button", { name: "Show initial password" }));
    expect(field).toHaveAttribute("type", "text");
    await user.click(within(dialog).getByRole("button", { name: "Generate password" }));
    expect((field as HTMLInputElement).value).toMatch(/^[a-hjkmnp-zA-HJ-NP-Z2-9]{4}(-[a-hjkmnp-zA-HJ-NP-Z2-9]{4}){3}$/);
    expect(dialog).toHaveTextContent("Password generated");
  });

  it("checks the length before sending anything", async () => {
    const api = mockApi({ [LIST]: page([]) });
    renderPage();
    const { dialog } = await fillCreateForm({ password: "short" });
    expect(within(dialog).getByLabelText("Initial password")).toHaveAccessibleDescription(
      expect.stringContaining("Use at least 12 characters."),
    );
    expect(within(dialog).getByLabelText("Initial password")).toHaveFocus();
    expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(0);
  });

  it("shows the server's password rule under the password field", async () => {
    mockApi({
      [LIST]: page([]),
      "POST /api/v1/admin/users": apiError(400, "validation_error", "Invalid input.", {
        password: ["This password is too common."],
      }),
    });
    renderPage();
    const { dialog } = await fillCreateForm();
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Initial password")).toHaveAccessibleDescription(
        expect.stringContaining("This password is too common."),
      ),
    );
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "New user" })).toBeInTheDocument();
  });

  it("can email an invitation instead (no password is sent)", async () => {
    const api = mockApi({
      [LIST]: page([rahul]),
      "POST /api/v1/admin/users": { status: 201, body: { ...invited, email: "new@example.test", role: "sales_user" } },
    });
    renderPage();
    await fillCreateForm({ password: null });
    expect(await screen.findByText(/An invitation is on its way to new@example.test/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/admin/users")[0]!.body).toEqual({
      first_name: "Neha",
      last_name: "Verma",
      email: "new@example.test",
      role: "sales_user",
    });
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/admin/users").length).toBeGreaterThan(1));
  });

  it("switching to an invitation drops a typed password, and back again asks for one", async () => {
    mockApi({ [LIST]: page([]) });
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New user" }));
    const dialog = screen.getByRole("dialog", { name: "New user" });
    await user.type(within(dialog).getByLabelText("Initial password"), PASSWORD);
    const toggle = within(dialog).getByRole("button", { name: "Email an invitation instead" });
    await user.click(toggle);
    expect(within(dialog).queryByLabelText("Initial password")).not.toBeInTheDocument();
    expect(toggle).toHaveTextContent("Set a password instead");
    expect(toggle).toHaveFocus();
    await user.click(toggle);
    expect(within(dialog).getByLabelText("Initial password")).toHaveValue("");
  });

  it("shows a duplicate email on the email field", async () => {
    mockApi({
      [LIST]: page([rahul]),
      "POST /api/v1/admin/users": apiError(409, "conflict", "A user with this email address already exists.", {
        email: ["A user with this email address already exists."],
      }),
    });
    renderPage();
    const { dialog } = await fillCreateForm({ email: "rahul@example.test" });
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Email")).toHaveAccessibleDescription(
        expect.stringContaining("already exists"),
      ),
    );
    expect(screen.getByRole("dialog", { name: "New user" })).toBeInTheDocument();
  });

  it("closes with Escape and returns focus to the button", async () => {
    mockApi({ [LIST]: page([]) });
    renderPage();
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "New user" });
    await user.click(button);
    expect(screen.getByLabelText("First name")).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(button).toHaveFocus();
  });
});

describe("UsersPage: user details", () => {
  const DETAIL = `GET /api/v1/admin/users/${rahul.id}`;
  const SET_PASSWORD = `POST /api/v1/admin/users/${rahul.id}/set-password`;
  const NEW_PASSWORD = "copper-meadow-skylark";

  async function openDetails(name = "Rahul Sharma") {
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name }));
    return { user, drawer: screen.getByRole("dialog", { name }) };
  }

  it("opens from the name with the user's details, and never a password", async () => {
    mockApi({ [LIST]: page([rahul]), [DETAIL]: { status: 200, body: rahul } });
    renderPage();
    const { user, drawer } = await openDetails();
    expect(drawer).toHaveTextContent("rahul@example.test");
    expect(drawer).toHaveTextContent("RoleUser");
    expect(drawer).toHaveTextContent("Active");
    expect(drawer).toHaveTextContent(`Set ${formatDate(rahul.password_changed_at)}`);
    expect(drawer).toHaveTextContent("Passwords are never shown.");
    expect(drawer.querySelector("input")).toBeNull();
    expect(within(drawer).getByRole("link", { name: "Open CRM" })).toHaveAttribute("href", `/admin/users/${rahul.id}/dashboard`);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rahul Sharma" })).toHaveFocus();
  });

  it("says when a password must be changed, or an invitation is still pending", async () => {
    const temporary = makeAdminUser({ password_change_required: true });
    mockApi({
      [LIST]: page([temporary, invited]),
      [DETAIL]: { status: 200, body: temporary },
      [`GET /api/v1/admin/users/${invited.id}`]: { status: 200, body: invited },
    });
    renderPage();
    let { user, drawer } = await openDetails();
    expect(drawer).toHaveTextContent("Must be changed at next sign-in");
    await user.keyboard("{Escape}");
    ({ user, drawer } = await openDetails("Neha Verma"));
    expect(drawer).toHaveTextContent("Invitation pending");
    expect(drawer).toHaveTextContent("Not available until they accept their invitation.");
    expect(within(drawer).queryByRole("button", { name: "Set new password" })).not.toBeInTheDocument();
  });

  it("sets a new password with the user's version, then shows what happens next", async () => {
    let current = rahul;
    const api = mockApi({
      [LIST]: () => page([current]),
      [DETAIL]: () => ({ status: 200, body: current }),
      [SET_PASSWORD]: () => {
        current = { ...rahul, password_change_required: true, version: 2 };
        return { status: 200, body: current };
      },
    });
    renderPage();
    const { user, drawer } = await openDetails();
    await user.click(within(drawer).getByRole("button", { name: "Set new password" }));
    const dialog = screen.getByRole("dialog", { name: "Set a new password for Rahul Sharma" });
    expect(within(dialog).getByLabelText("New password")).toHaveAttribute("autocomplete", "off");
    await user.type(within(dialog).getByLabelText("New password"), NEW_PASSWORD);
    await user.type(within(dialog).getByLabelText("Confirm password"), NEW_PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Set password" }));

    const back = await screen.findByRole("dialog", { name: "Rahul Sharma" });
    expect(await within(back).findByRole("status")).toHaveTextContent("Rahul Sharma must choose a new password at next sign-in.");
    expect(back).toHaveTextContent("Must be changed at next sign-in");
    expect(api.callsTo("POST", `/api/v1/admin/users/${rahul.id}/set-password`)[0]!.body).toEqual({
      version: 1,
      new_password: NEW_PASSWORD,
    });
    expect(document.body.innerHTML).not.toContain(NEW_PASSWORD);
  });

  it("checks that the two passwords match before sending", async () => {
    const api = mockApi({ [LIST]: page([rahul]), [DETAIL]: { status: 200, body: rahul } });
    renderPage();
    const { user, drawer } = await openDetails();
    await user.click(within(drawer).getByRole("button", { name: "Set new password" }));
    const dialog = screen.getByRole("dialog", { name: "Set a new password for Rahul Sharma" });
    await user.type(within(dialog).getByLabelText("New password"), NEW_PASSWORD);
    await user.type(within(dialog).getByLabelText("Confirm password"), "something-else-entirely");
    await user.click(within(dialog).getByRole("button", { name: "Set password" }));
    expect(within(dialog).getByLabelText("Confirm password")).toHaveAccessibleDescription("The passwords don't match.");
    expect(api.callsTo("POST", `/api/v1/admin/users/${rahul.id}/set-password`)).toHaveLength(0);
  });

  it.each([
    [422, "unprocessable", "A password can't be set for this user.", "A password can't be set for this user."],
    [409, "conflict", "The record was changed by someone else.", "Someone else changed this user meanwhile."],
  ])("explains a refusal (%s)", async (status, code, message, shown) => {
    mockApi({ [LIST]: page([rahul]), [DETAIL]: { status: 200, body: rahul }, [SET_PASSWORD]: apiError(status, code, message) });
    renderPage();
    const { user, drawer } = await openDetails();
    await user.click(within(drawer).getByRole("button", { name: "Set new password" }));
    const dialog = screen.getByRole("dialog", { name: "Set a new password for Rahul Sharma" });
    await user.type(within(dialog).getByLabelText("New password"), NEW_PASSWORD);
    await user.type(within(dialog).getByLabelText("Confirm password"), NEW_PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Set password" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(shown);
  });

  it.each([
    ["yourself", self, "This is you."],
    ["another administrator", makeAdminUser({ role: "admin", role_label: "Admin" }), "Not available for administrators."],
    ["a deactivated user", makeAdminUser({ status: "deactivated", status_label: "Deactivated" }), "Not available for deactivated users."],
  ])("offers no password or support tools for %s", async (_who, target, reason) => {
    mockApi({ [LIST]: page([target]), [`GET /api/v1/admin/users/${target.id}`]: { status: 200, body: target } });
    renderPage();
    const { drawer } = await openDetails(target.full_name);
    expect(drawer).toHaveTextContent(reason);
    expect(within(drawer).queryByRole("button", { name: "Set new password" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Access as user" })).not.toBeInTheDocument();
  });

  it("starts a support session and opens the user's CRM", async () => {
    const SESSION = {
      id: "0a1b2c3d-0000-4000-8000-000000000001",
      target: { id: rahul.id, full_name: "Rahul Sharma" },
      reason: "Fixing a lead",
      started_at: "2026-10-05T10:00:00Z",
      expires_at: "2026-10-05T10:30:00Z",
    };
    const api = mockApi({
      [LIST]: page([rahul]),
      [DETAIL]: { status: 200, body: rahul },
      "POST /api/v1/admin/support-sessions": { status: 201, body: SESSION },
      "GET /api/v1/auth/me": apiError(503, "service_unavailable", "Down."),
    });
    const client = createTestQueryClient();
    client.setQueryData(VIEWER_QUERY_KEY, adminViewer);
    renderWithProviders(<UsersPage />, { viewer: adminViewer, client });
    const { user, drawer } = await openDetails();
    await user.click(within(drawer).getByRole("button", { name: "Access as user" }));
    const dialog = screen.getByRole("dialog", { name: "Access Rahul Sharma's CRM?" });
    await user.type(within(dialog).getByLabelText(/Reason/), "Fixing a lead");
    await user.click(within(dialog).getByRole("button", { name: "Start support session" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/admin/users/${rahul.id}/dashboard`));
    expect(api.callsTo("POST", "/api/v1/admin/support-sessions")[0]!.body).toEqual({ user: rahul.id, reason: "Fixing a lead" });
    expect(client.getQueryData<Viewer>(VIEWER_QUERY_KEY)?.supportSession).toEqual({
      id: SESSION.id,
      target: { id: rahul.id, fullName: "Rahul Sharma" },
      reason: "Fixing a lead",
      startedAt: SESSION.started_at,
      expiresAt: SESSION.expires_at,
    });
  });

  it("explains why a support session couldn't start", async () => {
    mockApi({
      [LIST]: page([rahul]),
      [DETAIL]: { status: 200, body: rahul },
      "POST /api/v1/admin/support-sessions": apiError(409, "conflict", "You are already in a support session."),
    });
    renderPage();
    const { user, drawer } = await openDetails();
    await user.click(within(drawer).getByRole("button", { name: "Access as user" }));
    const dialog = screen.getByRole("dialog", { name: "Access Rahul Sharma's CRM?" });
    await user.click(within(dialog).getByRole("button", { name: "Start support session" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("You are already in a support session.");
    expect(nav.push).not.toHaveBeenCalled();
  });
});

describe("UsersPage: after a support session", () => {
  it("says the session ended when it brought the administrator back here", async () => {
    mockApi({ [LIST]: page([rahul]) });
    setFlash("Support session ended", "/admin/users");
    renderPage();
    expect(await screen.findByRole("status")).toHaveTextContent("Support session ended");
  });
});

describe("UsersPage: editing and lifecycle", () => {
  it("edits only changed fields and sends the version", async () => {
    const api = mockApi({
      [LIST]: page([rahul]),
      [`PATCH /api/v1/admin/users/${rahul.id}`]: { status: 200, body: { ...rahul, first_name: "Rahul K", version: 2 } },
    });
    renderPage();
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    const firstName = screen.getByLabelText("First name");
    await user.clear(firstName);
    await user.type(firstName, "Rahul K");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.callsTo("PATCH", `/api/v1/admin/users/${rahul.id}`)[0]!.body).toEqual({ first_name: "Rahul K", version: 1 });
  });

  it("explains a concurrent edit (409)", async () => {
    mockApi({
      [LIST]: page([rahul]),
      [`PATCH /api/v1/admin/users/${rahul.id}`]: apiError(409, "conflict", "The record was changed by someone else."),
    });
    renderPage();
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    await user.selectOptions(screen.getByLabelText("Role"), "admin");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Someone else changed this user");
  });

  it("does not let admins change their own role or deactivate themselves", async () => {
    mockApi({ [LIST]: page([self]) });
    renderPage();
    const user = await openActions("Anita Admin");
    expect(screen.queryByRole("menuitem", { name: "Deactivate" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    expect(screen.getByLabelText("Role")).toBeDisabled();
    expect(screen.getByLabelText("Role")).toHaveAccessibleDescription("You can't change your own role.");
  });

  it("deactivates after an explicit confirmation", async () => {
    const api = mockApi({
      [LIST]: page([rahul]),
      [`POST /api/v1/admin/users/${rahul.id}/deactivate`]: {
        status: 200,
        body: { ...rahul, status: "deactivated", status_label: "Deactivated" },
      },
    });
    renderPage();
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Deactivate" }));
    const dialog = screen.getByRole("alertdialog", { name: "Deactivate Rahul Sharma?" });
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("signed out everywhere immediately"));
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus(); // not the destructive one
    expect(api.callsTo("POST", `/api/v1/admin/users/${rahul.id}/deactivate`)).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Deactivate" }));
    expect(await screen.findByText("Rahul Sharma was deactivated.")).toBeInTheDocument();
    expect(api.callsTo("POST", `/api/v1/admin/users/${rahul.id}/deactivate`)).toHaveLength(1);
  });

  it("can cancel a confirmation without calling the API", async () => {
    const api = mockApi({ [LIST]: page([rahul]) });
    renderPage();
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Deactivate" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("offers resend only for invited users and reports the cooldown", async () => {
    mockApi({
      [LIST]: page([rahul, invited]),
      [`POST /api/v1/admin/users/${invited.id}/resend-invitation`]: apiError(
        429,
        "rate_limited",
        "An invitation was sent moments ago. Wait a minute before sending another.",
      ),
    });
    renderPage();
    let user = await openActions("Rahul Sharma");
    expect(screen.queryByRole("menuitem", { name: "Resend invitation" })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    user = await openActions("Neha Verma");
    await user.click(screen.getByRole("menuitem", { name: "Resend invitation" }));
    await user.click(screen.getByRole("button", { name: "Resend invitation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("An invitation was sent moments ago");
  });

  it("offers reactivation for deactivated users", async () => {
    const gone = makeAdminUser({ status: "deactivated", status_label: "Deactivated" });
    mockApi({
      [LIST]: page([gone]),
      [`POST /api/v1/admin/users/${gone.id}/activate`]: { status: 200, body: { ...gone, status: "active" } },
    });
    renderPage();
    const user = await openActions("Rahul Sharma");
    expect(screen.queryByRole("menuitem", { name: "Deactivate" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Reactivate" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("existing password");
    await user.click(screen.getByRole("button", { name: "Reactivate" }));
    expect(await screen.findByText("Rahul Sharma was reactivated.")).toBeInTheDocument();
  });

  describe("another administrator's account (only they change its email, password and role)", () => {
    const bina = makeAdminUser({
      id: "3b2a1c0d-2222-4333-8444-555566667777",
      email: "bina@example.test",
      first_name: "Bina",
      last_name: "Admin",
      full_name: "Bina Admin",
      role: "admin",
      role_label: "Admin",
    });

    it.each([
      ["active", bina],
      ["deactivated", { ...bina, status: "deactivated" as const, status_label: "Deactivated" }],
      ["invited", { ...bina, status: "invited" as const, status_label: "Invited", last_login: null }],
    ])("offers no email change, whatever the status (%s)", async (_status, target) => {
      mockApi({ [LIST]: page([target]) });
      renderPage();
      await openActions("Bina Admin");
      expect(screen.getByRole("menuitem", { name: "Edit details" })).toBeInTheDocument();
      expect(screen.queryByRole("menuitem", { name: "Change email" })).not.toBeInTheDocument();
    });

    it("still offers your own email change, and deactivation of another administrator", async () => {
      mockApi({ [LIST]: page([self, bina]) });
      renderPage();
      let user = await openActions("Anita Admin");
      expect(screen.getByRole("menuitem", { name: "Change email" })).toBeInTheDocument();
      await user.keyboard("{Escape}");
      user = await openActions("Bina Admin");
      expect(screen.getByRole("menuitem", { name: "Deactivate" })).toBeInTheDocument();
    });

    it("locks the role with the reason, while the name can still be corrected", async () => {
      const api = mockApi({
        [LIST]: page([bina]),
        [`PATCH /api/v1/admin/users/${bina.id}`]: { status: 200, body: { ...bina, last_name: "Kapoor", version: 2 } },
      });
      renderPage();
      const user = await openActions("Bina Admin");
      await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
      const role = screen.getByLabelText("Role");
      expect(role).toBeDisabled();
      expect(role).toHaveAccessibleDescription(
        "An administrator's role can't be changed here. To remove their access, deactivate the account.",
      );
      const lastName = screen.getByLabelText(/Last name/);
      await user.clear(lastName);
      await user.type(lastName, "Kapoor");
      await user.click(screen.getByRole("button", { name: "Save changes" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      expect(api.callsTo("PATCH", `/api/v1/admin/users/${bina.id}`)[0]!.body).toEqual({ last_name: "Kapoor", version: 1 });
    });

    it("offers no password or support tools in the details panel", async () => {
      mockApi({ [LIST]: page([bina]), [`GET /api/v1/admin/users/${bina.id}`]: { status: 200, body: bina } });
      renderPage();
      await userEvent.setup().click(await screen.findByRole("button", { name: "Bina Admin" }));
      const drawer = screen.getByRole("dialog", { name: "Bina Admin" });
      expect(drawer).toHaveTextContent("Not available for administrators.");
      expect(within(drawer).queryByRole("button", { name: "Set new password" })).not.toBeInTheDocument();
    });

    // The list said "User", but they were made an administrator meanwhile: the server's
    // reason is shown, and the list is fetched again.
    it.each([
      ["change-email", "Administrators change their own email address in Settings."],
      ["role", "An administrator's role can't be changed by another administrator. To remove their access, deactivate the account."],
    ])("shows the server's refusal when a stale page tries anyway (%s)", async (what, message) => {
      const api = mockApi({
        [LIST]: page([rahul]),
        [`POST /api/v1/admin/users/${rahul.id}/change-email`]: apiError(422, "business_rule_violation", message),
        [`PATCH /api/v1/admin/users/${rahul.id}`]: apiError(422, "business_rule_violation", message),
      });
      renderPage();
      const user = await openActions("Rahul Sharma");
      if (what === "change-email") {
        await user.click(screen.getByRole("menuitem", { name: "Change email" }));
        const field = screen.getByLabelText("New email");
        await user.clear(field);
        await user.type(field, "elsewhere@example.test");
        await user.click(screen.getByRole("button", { name: "Change email" }));
      } else {
        await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
        await user.selectOptions(screen.getByLabelText("Role"), "admin");
        await user.click(screen.getByRole("button", { name: "Save changes" }));
      }
      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      await waitFor(() => expect(api.callsTo("GET", "/api/v1/admin/users").length).toBeGreaterThan(1));
    });
  });

  it("changes email as a separate, explained action", async () => {
    const api = mockApi({
      [LIST]: page([rahul]),
      [`POST /api/v1/admin/users/${rahul.id}/change-email`]: { status: 200, body: { ...rahul, email: "rahul.k@example.test" } },
    });
    renderPage();
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Change email" }));
    const dialog = screen.getByRole("dialog", { name: "Change email for Rahul Sharma" });
    expect(dialog).toHaveTextContent("signed out everywhere");
    const field = within(dialog).getByLabelText("New email");
    await user.clear(field);
    await user.type(field, "rahul.k@example.test");
    await user.click(within(dialog).getByRole("button", { name: "Change email" }));
    expect(await screen.findByText("Rahul Sharma now signs in with rahul.k@example.test.")).toBeInTheDocument();
    expect(api.callsTo("POST", `/api/v1/admin/users/${rahul.id}/change-email`)[0]!.body).toEqual({
      email: "rahul.k@example.test",
      version: 1,
    });
  });
});

describe("row action menu keyboard support", () => {
  it("opens with ArrowDown, moves with arrows and closes with Escape", async () => {
    mockApi({ [LIST]: page([rahul]) });
    renderPage();
    const trigger = await screen.findByRole("button", { name: "Actions for Rahul Sharma" });
    const user = userEvent.setup();
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Edit details" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Change email" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("menuitem", { name: "Deactivate" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
