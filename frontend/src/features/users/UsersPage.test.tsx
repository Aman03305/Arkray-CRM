import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { AdminUser } from "@/lib/api/types";
import { adminViewer, makeAdminUser } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { UsersPage } from "./UsersPage";

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

    const row = (await screen.findByRole("link", { name: "Rahul Sharma, open CRM workspace" })).closest("tr")!;
    expect(within(row).getByText("rahul@example.test")).toBeInTheDocument();
    expect(within(row).getByText("User")).toBeInTheDocument();
    expect(within(row).getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Rahul Sharma, open CRM workspace" })).toHaveAttribute(
      "href",
      `/admin/users/${rahul.id}/dashboard`,
    );
    const invitedRow = screen.getByRole("link", { name: "Neha Verma, open CRM workspace" }).closest("tr")!;
    expect(within(invitedRow).getByText("Invited")).toBeInTheDocument();
    expect(within(invitedRow).getByText(/Link expires/)).toBeInTheDocument();
    expect(within(invitedRow).getByText("Never")).toBeInTheDocument();
    expect(screen.getByText("(you)")).toBeInTheDocument();
  });

  it("shows the table columns the brief asks for, and no invented CRM metrics", async () => {
    mockApi({ [LIST]: page([rahul]) });
    renderPage();
    await screen.findByRole("link", { name: "Rahul Sharma, open CRM workspace" });
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
    await screen.findByRole("link", { name: "Rahul Sharma, open CRM workspace" });
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Filter by status"), "deactivated");
    expect(await screen.findByText("No users match your filters")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("link", { name: "Rahul Sharma, open CRM workspace" })).toBeInTheDocument();
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
    expect(await screen.findByRole("link", { name: "Rahul Sharma, open CRM workspace" })).toBeInTheDocument();
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
    await screen.findByRole("link", { name: "Rahul Sharma, open CRM workspace" });
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
    await screen.findByRole("link", { name: "Rahul Sharma, open CRM workspace" });
    const user = userEvent.setup();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("link", { name: "Neha Verma, open CRM workspace" })).toBeInTheDocument();
    expect(api.fetchMock.mock.calls.every(([url]) => String(url).startsWith("/api/"))).toBe(true);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });
});

describe("UsersPage: creating a user", () => {
  async function fillCreateForm(email = "new@example.test") {
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New user" }));
    const dialog = screen.getByRole("dialog", { name: "New user" });
    await user.type(within(dialog).getByLabelText("First name"), "Neha");
    await user.type(within(dialog).getByLabelText(/Last name/), "Verma");
    await user.type(within(dialog).getByLabelText("Email"), email);
    await user.selectOptions(within(dialog).getByLabelText("Role"), "admin");
    await user.click(within(dialog).getByRole("button", { name: "Create and invite" }));
    return dialog;
  }

  it("creates an invited user (no password field) and confirms the invitation", async () => {
    const api = mockApi({
      [LIST]: page([rahul]),
      "POST /api/v1/admin/users": { status: 201, body: { ...invited, email: "new@example.test", role: "admin" } },
    });
    renderPage();
    await fillCreateForm();
    expect(await screen.findByText(/An invitation is on its way to new@example.test/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/admin/users")[0]!.body).toEqual({
      first_name: "Neha",
      last_name: "Verma",
      email: "new@example.test",
      role: "admin",
    });
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/admin/users").length).toBeGreaterThan(1));
  });

  it("shows a duplicate email on the email field", async () => {
    mockApi({
      [LIST]: page([rahul]),
      "POST /api/v1/admin/users": apiError(409, "conflict", "A user with this email address already exists.", {
        email: ["A user with this email address already exists."],
      }),
    });
    renderPage();
    const dialog = await fillCreateForm("rahul@example.test");
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Email")).toHaveAccessibleDescription(
        expect.stringContaining("already exists"),
      ),
    );
    expect(screen.getByRole("dialog", { name: "New user" })).toBeInTheDocument();
  });

  it("never asks the admin for the user's password", async () => {
    mockApi({ [LIST]: page([]) });
    renderPage();
    await userEvent.setup().click(await screen.findByRole("button", { name: "New user" }));
    expect(within(screen.getByRole("dialog")).queryByLabelText(/password/i)).not.toBeInTheDocument();
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
