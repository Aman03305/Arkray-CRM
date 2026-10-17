/**
 * Super admins and Admins on the Users page (ADR-0035). A Super admin chooses roles (User,
 * Admin, Super admin) and each User's Admin, sees whose team everyone is in, and never gets to
 * change another Super admin's role. An Admin (team administrator) sees "My team" and creates
 * Users only: no role or Admin is ever chosen or sent. The UI only decides what to show; the
 * API refuses the rest (identity tests).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AdminUser } from "@/lib/api/types";
import type { Viewer } from "@/lib/viewer";
import { adminViewer, makeAdminUser, teamAdminViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { UsersPage } from "./UsersPage";

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/users",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const LIST = "GET /api/v1/admin/users";
const CREATE = "POST /api/v1/admin/users";

const tara = makeAdminUser({
  id: teamAdminViewer.id,
  email: "tara@example.test",
  first_name: "Tara",
  last_name: "Singh",
  full_name: "Tara Singh",
  role: "team_admin",
  role_label: "Admin",
});
const omar = makeAdminUser({
  id: "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
  email: "omar@example.test",
  first_name: "Omar",
  last_name: "Khan",
  full_name: "Omar Khan",
  role: "team_admin",
  role_label: "Admin",
  status: "invited",
  status_label: "Invited",
  last_login: null,
  activated_at: null,
});
const dev = makeAdminUser({
  id: "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
  email: "dev@example.test",
  first_name: "Dev",
  last_name: "Rao",
  full_name: "Dev Rao",
  role: "team_admin",
  role_label: "Admin",
  status: "deactivated",
  status_label: "Deactivated",
});
const rahul = makeAdminUser({ manager: { id: tara.id, full_name: "Tara Singh" } });
const priya = makeAdminUser({
  id: "5c4b3a29-1807-4f6e-9d5c-4b3a29180716",
  email: "priya@example.test",
  first_name: "Priya",
  last_name: "Patel",
  full_name: "Priya Patel",
});
const bina = makeAdminUser({
  id: "3b2a1c0d-2222-4333-8444-555566667777",
  email: "bina@example.test",
  first_name: "Bina",
  last_name: "Kapoor",
  full_name: "Bina Kapoor",
  role: "admin",
  role_label: "Super admin",
});

function page(results: AdminUser[]) {
  return { status: 200, body: { results, next: null, previous: null } };
}

/** The list, and the Admins a Super admin's page loads for its pickers (role=team_admin). */
function listRoute(users: AdminUser[]) {
  return (call: RecordedCall) => (call.query.get("role") === "team_admin" ? page([tara, omar, dev]) : page(users));
}

function renderAs(viewer: Viewer) {
  return renderWithProviders(<UsersPage />, { viewer });
}

const listCalls = (api: ReturnType<typeof mockApi>) =>
  api.callsTo("GET", "/api/v1/admin/users").filter((c) => c.query.get("role") !== "team_admin");

async function openNewUser() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "New user" }));
  const dialog = screen.getByRole("dialog", { name: "New user" });
  await user.type(within(dialog).getByLabelText("First name"), "Neha");
  await user.type(within(dialog).getByLabelText(/Last name/), "Verma");
  await user.type(within(dialog).getByLabelText("Email"), "neha@example.test");
  return { user, dialog };
}

const optionLabels = (select: HTMLElement) => within(select).getAllByRole("option").map((o) => o.textContent);

describe("a Super admin", () => {
  it("sees each user's Admin, and filters by Admin (one, or none)", async () => {
    const api = mockApi({ [LIST]: listRoute([rahul, priya, tara]) });
    renderAs(adminViewer);
    const rahulRow = (await screen.findByRole("button", { name: "Rahul Sharma" })).closest("tr")!;
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toContain("Admin");
    expect(within(rahulRow).getByText("Tara Singh")).toBeInTheDocument();
    const priyaRow = screen.getByRole("button", { name: "Priya Patel" }).closest("tr")!;
    expect(within(priyaRow).getByText("—")).toBeInTheDocument();
    const taraRow = screen.getByRole("button", { name: "Tara Singh" }).closest("tr")!;
    expect(within(taraRow).getByText("Admin")).toBeInTheDocument(); // her role's label

    const filter = screen.getByLabelText("Filter by Admin");
    // By name (the API lists them newest first).
    await waitFor(() => expect(optionLabels(filter)).toEqual(["Any Admin", "No admin", "Dev Rao (deactivated)", "Omar Khan", "Tara Singh"]));
    const user = userEvent.setup();
    await user.selectOptions(filter, "No admin");
    await waitFor(() => expect(listCalls(api).at(-1)!.query.get("manager")).toBe("none"));
    await user.selectOptions(filter, "Tara Singh");
    await waitFor(() => expect(listCalls(api).at(-1)!.query.get("manager")).toBe(tara.id));
    // A deactivated Admin's users stay listed under them: they can be found too.
    await user.selectOptions(filter, "Dev Rao (deactivated)");
    await waitFor(() => expect(listCalls(api).at(-1)!.query.get("manager")).toBe(dev.id));
  });

  it("filters by every role, Admin and Super admin included", async () => {
    const api = mockApi({ [LIST]: listRoute([rahul]) });
    renderAs(adminViewer);
    await screen.findByRole("button", { name: "Rahul Sharma" });
    const filter = screen.getByLabelText("Filter by role");
    expect(optionLabels(filter)).toEqual(["All roles", "User", "Admin", "Super admin"]);
    await userEvent.setup().selectOptions(filter, "Admin");
    // The list's own page (25), not the 100 Admins its pickers load.
    await waitFor(() =>
      expect(api.calls.some((c) => c.query.get("role") === "team_admin" && c.query.get("page_size") === "25")).toBe(true),
    );
  });

  it("creates a User in an active or invited Admin's team", async () => {
    const api = mockApi({ [LIST]: listRoute([rahul]), [CREATE]: { status: 201, body: { ...priya, manager: { id: omar.id, full_name: "Omar Khan" } } } });
    renderAs(adminViewer);
    const { user, dialog } = await openNewUser();
    expect(optionLabels(within(dialog).getByLabelText("Role"))).toEqual(["User", "Admin", "Super admin"]);
    const admin = within(dialog).getByLabelText("Admin");
    expect(admin).toHaveValue("");
    expect(admin).toHaveAccessibleDescription("Their Admin manages their account and can open their CRM.");
    // By name; never a deactivated Admin (the API refuses one).
    await waitFor(() =>
      expect(optionLabels(admin)).toEqual([
        "No admin (managed by Super admins)",
        "Omar Khan (omar@example.test, invited)",
        "Tara Singh (tara@example.test)",
      ]),
    );
    await user.selectOptions(admin, omar.id);
    await user.click(within(dialog).getByRole("button", { name: "Create and invite" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/admin/users")[0]!.body).toEqual({
      first_name: "Neha",
      last_name: "Verma",
      email: "neha@example.test",
      role: "sales_user",
      manager: omar.id,
    });
  });

  it("sends no Admin for a User managed by the Super admins", async () => {
    const api = mockApi({ [LIST]: listRoute([rahul]), [CREATE]: { status: 201, body: priya } });
    renderAs(adminViewer);
    const { user, dialog } = await openNewUser();
    await user.click(within(dialog).getByRole("button", { name: "Create and invite" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/admin/users")[0]!.body).not.toHaveProperty("manager");
  });

  it.each([
    ["team_admin", "Admin"],
    ["admin", "Super admin"],
  ] as const)("creates an %s by invitation only, with no Admin of their own", async (role, label) => {
    const api = mockApi({ [LIST]: listRoute([rahul]), [CREATE]: { status: 201, body: { ...tara, status: "invited" } } });
    renderAs(adminViewer);
    const { user, dialog } = await openNewUser();
    await user.selectOptions(within(dialog).getByLabelText("Admin"), tara.id);
    await user.selectOptions(within(dialog).getByLabelText("Role"), label);
    expect(within(dialog).queryByLabelText("Admin")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Set a password instead" })).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent("Administrators are invited");
    await user.click(within(dialog).getByRole("button", { name: "Create and invite" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/admin/users")[0]!.body).toEqual({
      first_name: "Neha",
      last_name: "Verma",
      email: "neha@example.test",
      role,
    });
  });

  it("shows the API's refusal of an Admin under the Admin field", async () => {
    mockApi({
      [LIST]: listRoute([rahul]),
      [CREATE]: apiError(400, "validation_error", "Invalid input.", { manager: ["Choose an active or invited Admin."] }),
    });
    renderAs(adminViewer);
    const { user, dialog } = await openNewUser();
    await user.selectOptions(within(dialog).getByLabelText("Admin"), tara.id);
    await user.click(within(dialog).getByRole("button", { name: "Create and invite" }));
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Admin")).toHaveAccessibleDescription(
        expect.stringContaining("Choose an active or invited Admin."),
      ),
    );
    // On the field it is about, not in a banner.
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("moves a User to another team from their details, then is back there with the outcome", async () => {
    const moved = { ...rahul, manager: null, version: 2 };
    let current: AdminUser = rahul;
    const api = mockApi({
      [LIST]: listRoute([rahul]),
      [`GET /api/v1/admin/users/${rahul.id}`]: () => ({ status: 200, body: current }),
      [`PATCH /api/v1/admin/users/${rahul.id}`]: () => {
        current = moved;
        return { status: 200, body: moved };
      },
    });
    renderAs(adminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Rahul Sharma" }));
    const drawer = screen.getByRole("dialog", { name: "Rahul Sharma" });
    expect(drawer).toHaveTextContent("AdminTara Singh");
    await user.click(within(drawer).getByRole("button", { name: "Change role or Admin" }));

    const dialog = screen.getByRole("dialog", { name: "Edit Rahul Sharma" });
    const admin = within(dialog).getByLabelText("Admin");
    expect(admin).toHaveValue(tara.id);
    await user.selectOptions(admin, "No admin (managed by Super admins)");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    const back = await screen.findByRole("dialog", { name: "Rahul Sharma" });
    expect(await within(back).findByRole("status")).toHaveTextContent("Rahul Sharma was updated.");
    expect(back).toHaveTextContent("AdminNo admin (managed by Super admins)");
    expect(api.callsTo("PATCH", `/api/v1/admin/users/${rahul.id}`)[0]!.body).toEqual({ manager: null, version: 1 });
  });

  it("promotes a User to Admin (their own Admin is dropped by the server, so none is sent)", async () => {
    const api = mockApi({
      [LIST]: listRoute([rahul]),
      [`PATCH /api/v1/admin/users/${rahul.id}`]: { status: 200, body: { ...rahul, role: "team_admin", manager: null, version: 2 } },
    });
    renderAs(adminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Rahul Sharma" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    const dialog = screen.getByRole("dialog", { name: "Edit Rahul Sharma" });
    await user.selectOptions(within(dialog).getByLabelText("Role"), "Admin");
    expect(within(dialog).queryByLabelText("Admin")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.callsTo("PATCH", `/api/v1/admin/users/${rahul.id}`)[0]!.body).toEqual({ role: "team_admin", version: 1 });
  });

  it("makes an Admin a User in someone else's team in one change, saying what happens to their team", async () => {
    const api = mockApi({
      [LIST]: listRoute([tara]),
      [`GET /api/v1/admin/users/${tara.id}`]: { status: 200, body: tara },
      [`PATCH /api/v1/admin/users/${tara.id}`]: { status: 200, body: { ...tara, role: "sales_user", version: 2 } },
    });
    renderAs(adminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Tara Singh" }));
    const drawer = screen.getByRole("dialog", { name: "Tara Singh" });
    expect(drawer).not.toHaveTextContent("AdminNo admin"); // an Admin has no Admin of their own
    await user.click(within(drawer).getByRole("button", { name: "Change role" }));
    const dialog = screen.getByRole("dialog", { name: "Edit Tara Singh" });
    const role = within(dialog).getByLabelText("Role");
    expect(optionLabels(role)).toEqual(["User", "Admin", "Super admin"]);
    await user.selectOptions(role, "User");
    expect(role).toHaveAccessibleDescription(expect.stringContaining("Their users will have no Admin until you choose one"));
    await user.selectOptions(within(dialog).getByLabelText("Admin"), omar.id);
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.callsTo("PATCH", `/api/v1/admin/users/${tara.id}`)).toHaveLength(1));
    expect(api.callsTo("PATCH", `/api/v1/admin/users/${tara.id}`)[0]!.body).toEqual({ role: "sales_user", manager: omar.id, version: 1 });
  });

  it("is never offered another Super admin's role change: deactivating is the way", async () => {
    const api = mockApi({ [LIST]: listRoute([bina]), [`GET /api/v1/admin/users/${bina.id}`]: { status: 200, body: bina } });
    renderAs(adminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Bina Kapoor" }));
    const drawer = screen.getByRole("dialog", { name: "Bina Kapoor" });
    expect(within(drawer).queryByRole("button", { name: /Change role/ })).not.toBeInTheDocument();
    expect(drawer).toHaveTextContent("A Super admin's role can't be changed. To remove their access, deactivate the account.");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Actions for Bina Kapoor" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    expect(screen.getByLabelText("Role")).toBeDisabled();
    expect(api.callsTo("PATCH", `/api/v1/admin/users/${bina.id}`)).toHaveLength(0);
  });

  it("nor their own role", async () => {
    const self = makeAdminUser({ id: adminViewer.id, full_name: "Anita Admin", first_name: "Anita", last_name: "Admin", role: "admin", role_label: "Super admin" });
    mockApi({ [LIST]: listRoute([self]), [`GET /api/v1/admin/users/${self.id}`]: { status: 200, body: self } });
    renderAs(adminViewer);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Anita Admin" }));
    const drawer = screen.getByRole("dialog", { name: "Anita Admin" });
    expect(within(drawer).queryByRole("button", { name: /Change role/ })).not.toBeInTheDocument();
    expect(drawer).toHaveTextContent("You can't change your own role.");
  });

  it("treats an Admin's credentials like any administrator's: only they change them", async () => {
    const withMfa = { ...tara, mfa_enabled: true };
    mockApi({ [LIST]: listRoute([withMfa]), [`GET /api/v1/admin/users/${tara.id}`]: { status: 200, body: withMfa } });
    renderAs(adminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Tara Singh" }));
    expect(screen.getByRole("menuitem", { name: "Edit details" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Change email" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Reset two-step verification" })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Tara Singh" }));
    const drawer = screen.getByRole("dialog", { name: "Tara Singh" });
    expect(drawer).toHaveTextContent("Not available for administrators.");
    expect(within(drawer).queryByRole("button", { name: "Set temporary password" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Pseudonymise…" })).not.toBeInTheDocument();
  });

  it("is told an Admin's team stays put when the Admin is deactivated", async () => {
    mockApi({ [LIST]: listRoute([tara]) });
    renderAs(adminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Tara Singh" }));
    await user.click(screen.getByRole("menuitem", { name: "Deactivate" }));
    expect(screen.getByRole("alertdialog", { name: "Deactivate Tara Singh?" })).toHaveTextContent(
      "Their users stay in their team, managed only by Super admins until you give them another Admin.",
    );
  });
});

describe("an Admin (team administrator)", () => {
  const self = tara;

  it("sees My team: themselves and their users, with no role or Admin filters and no Admin column", async () => {
    const api = mockApi({ [LIST]: page([self, rahul]) });
    renderAs(teamAdminViewer);
    expect(screen.getByRole("heading", { level: 1, name: "My team" })).toBeInTheDocument();
    expect(screen.getByText("You and the users you manage.")).toBeInTheDocument();
    await screen.findByRole("button", { name: "Rahul Sharma" });
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Name",
      "Email",
      "Role",
      "Status",
      "Two-step",
      "Last login",
      "Created",
      "Actions",
    ]);
    expect(screen.queryByLabelText("Filter by role")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Filter by Admin")).not.toBeInTheDocument();
    // The Admins are never asked for (a Super admin's list).
    expect(api.calls.some((c) => c.query.get("role") === "team_admin")).toBe(false);
    // Their users' CRM (workspace.view_team).
    expect(screen.getByRole("link", { name: "Open CRM for Rahul Sharma" })).toHaveAttribute("href", `/admin/users/${rahul.id}/dashboard`);
  });

  it("creates a User in their own team: no role or Admin is chosen or sent", async () => {
    const api = mockApi({ [LIST]: page([self, rahul]), [CREATE]: { status: 201, body: priya } });
    renderAs(teamAdminViewer);
    const { user, dialog } = await openNewUser();
    expect(within(dialog).queryByLabelText("Role")).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Admin")).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent("New users join your team: you manage their account and can open their CRM.");
    // A User may start with an initial password, as before.
    expect(within(dialog).getByRole("button", { name: "Set a password instead" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create and invite" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/admin/users")[0]!.body).toEqual({
      first_name: "Neha",
      last_name: "Verma",
      email: "neha@example.test",
      role: "sales_user",
    });
  });

  it("edits a team member's name only: role and Admin are never offered or sent", async () => {
    const api = mockApi({
      [LIST]: page([self, rahul]),
      [`PATCH /api/v1/admin/users/${rahul.id}`]: { status: 200, body: { ...rahul, first_name: "Rahul K", version: 2 } },
    });
    renderAs(teamAdminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Rahul Sharma" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    const dialog = screen.getByRole("dialog", { name: "Edit Rahul Sharma" });
    expect(within(dialog).queryByLabelText("Role")).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Admin")).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("First name"), " K");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.callsTo("PATCH", `/api/v1/admin/users/${rahul.id}`)).toHaveLength(1));
    expect(api.callsTo("PATCH", `/api/v1/admin/users/${rahul.id}`)[0]!.body).toEqual({ first_name: "Rahul K", version: 1 });
  });

  it("has a team member's sign-in and support tools, but no role, Admin or privacy controls", async () => {
    mockApi({ [LIST]: page([self, rahul]), [`GET /api/v1/admin/users/${rahul.id}`]: { status: 200, body: rahul } });
    renderAs(teamAdminViewer);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Rahul Sharma" }));
    const drawer = screen.getByRole("dialog", { name: "Rahul Sharma" });
    expect(within(drawer).getByRole("button", { name: "Set temporary password" })).toBeInTheDocument();
    // support.access_team
    expect(within(drawer).getByRole("button", { name: "Access as user" })).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: "Open CRM" })).toHaveAttribute("href", `/admin/users/${rahul.id}/dashboard`);
    expect(within(drawer).queryByRole("button", { name: /Change role/ })).not.toBeInTheDocument();
    expect(drawer).not.toHaveTextContent("AdminTara Singh");
    expect(within(drawer).queryByRole("button", { name: "Export data" })).not.toBeInTheDocument();
  });

  it("can't change their own role either (nothing is offered)", async () => {
    mockApi({ [LIST]: page([self, rahul]), [`GET /api/v1/admin/users/${self.id}`]: { status: 200, body: self } });
    renderAs(teamAdminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Tara Singh" }));
    expect(screen.queryByRole("menuitem", { name: "Deactivate" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    expect(within(screen.getByRole("dialog", { name: "Edit Tara Singh" })).queryByLabelText("Role")).not.toBeInTheDocument();
  });
});

describe("review of the Admin pickers (ADR-0035)", () => {
  const zara = makeAdminUser({
    id: "9a9a9a9a-1111-4222-8333-444455556666",
    email: "zara@example.test",
    first_name: "Zara",
    last_name: "Ali",
    full_name: "Zara Ali",
    role: "team_admin",
    role_label: "Admin",
  });

  it("never offers an Admin being made a User as their own Admin", async () => {
    mockApi({ [LIST]: listRoute([tara, omar]) });
    renderAs(adminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Tara Singh" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    const dialog = screen.getByRole("dialog", { name: "Edit Tara Singh" });
    await user.selectOptions(within(dialog).getByLabelText("Role"), "User");
    const admin = within(dialog).getByLabelText("Admin");
    await waitFor(() =>
      expect(optionLabels(admin)).toEqual(["No admin (managed by Super admins)", "Omar Khan (omar@example.test, invited)"]),
    );
  });

  it("with more than 100 Admins, the filter's search box narrows its choices server-side", async () => {
    const api = mockApi({
      [LIST]: (call: RecordedCall) => {
        if (call.query.get("role") !== "team_admin") return page([rahul]);
        if (call.query.get("q") === "Zar") return page([zara]);
        // A full page, and more beyond it.
        return { status: 200, body: { results: [tara, omar], next: "http://backend/api/v1/admin/users?cursor=p2", previous: null } };
      },
    });
    renderAs(adminViewer);
    await screen.findByRole("button", { name: "Rahul Sharma" });
    const user = userEvent.setup();
    const find = await screen.findByRole("searchbox", { name: "Find an Admin to filter by" });
    const filter = screen.getByLabelText("Filter by Admin");
    expect(optionLabels(filter)).not.toContain("Zara Ali");
    await user.type(find, "Zar");
    await waitFor(() => expect(optionLabels(filter)).toEqual(["Any Admin", "No admin", "Zara Ali"]));
    expect(api.calls.some((c) => c.query.get("role") === "team_admin" && c.query.get("q") === "Zar")).toBe(true);
    await user.selectOptions(filter, "Zara Ali");
    await waitFor(() => expect(listCalls(api).at(-1)!.query.get("manager")).toBe(zara.id));
    // Clearing the search keeps the chosen Admin shown (and applied).
    await user.clear(find);
    await waitFor(() => expect(optionLabels(filter)).toEqual(["Any Admin", "No admin", "Zara Ali", "Omar Khan", "Tara Singh"]));
    expect((filter as HTMLSelectElement).selectedOptions[0]!.textContent).toBe("Zara Ali");
  });

  it("with 100 Admins or fewer there is no search box", async () => {
    mockApi({ [LIST]: listRoute([rahul]) });
    renderAs(adminViewer);
    await screen.findByRole("button", { name: "Rahul Sharma" });
    await waitFor(() => expect(optionLabels(screen.getByLabelText("Filter by Admin"))).toContain("Tara Singh"));
    expect(screen.queryByRole("searchbox", { name: "Find an Admin to filter by" })).not.toBeInTheDocument();
  });

  it("keeps showing the Admin it filters by after they stop being an Admin elsewhere", async () => {
    let admins: AdminUser[] = [tara, omar];
    const api = mockApi({
      [LIST]: (call: RecordedCall) => (call.query.get("role") === "team_admin" ? page(admins) : page([rahul])),
    });
    const { client } = renderAs(adminViewer);
    await screen.findByRole("button", { name: "Rahul Sharma" });
    const filter = screen.getByLabelText("Filter by Admin") as HTMLSelectElement;
    await waitFor(() => expect(optionLabels(filter)).toContain("Tara Singh"));
    await userEvent.setup().selectOptions(filter, "Tara Singh");
    await waitFor(() => expect(listCalls(api).at(-1)!.query.get("manager")).toBe(tara.id));
    // Tara was made a User in another tab; any refresh reloads the Admins without her.
    admins = [omar];
    await client.invalidateQueries({ queryKey: ["admin-users"] });
    await waitFor(() => expect(optionLabels(filter)).toEqual(["Any Admin", "No admin", "Tara Singh", "Omar Khan"]));
    // What is shown is what the list applies.
    expect(filter.selectedOptions[0]!.textContent).toBe("Tara Singh");
    expect(filter).toHaveValue(tara.id);
    expect(listCalls(api).at(-1)!.query.get("manager")).toBe(tara.id);
  });
});

describe("a user who left the viewer's reach (404)", () => {
  const GONE =
    "Rahul Sharma is no longer in your reach (they may have moved to another team). Nothing was changed, and the list has been refreshed.";

  it("closes their details when the API no longer finds them, says why and reads the list again", async () => {
    const api = mockApi({
      [LIST]: page([tara, rahul]),
      [`GET /api/v1/admin/users/${rahul.id}`]: apiError(404, "not_found", "Not found."),
    });
    renderAs(teamAdminViewer);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Rahul Sharma" }));
    expect(await screen.findByRole("status")).toHaveTextContent(GONE);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set temporary password" })).not.toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/admin/users").length).toBeGreaterThan(1));
  });

  it("closes an action's dialog when the API answers 404, says why and reads the list again", async () => {
    const api = mockApi({
      [LIST]: page([tara, rahul]),
      [`POST /api/v1/admin/users/${rahul.id}/deactivate`]: apiError(404, "not_found", "Not found."),
    });
    renderAs(teamAdminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Rahul Sharma" }));
    await user.click(screen.getByRole("menuitem", { name: "Deactivate" }));
    const confirm = screen.getByRole("alertdialog", { name: "Deactivate Rahul Sharma?" });
    await user.click(within(confirm).getByRole("button", { name: "Deactivate" }));
    expect(await screen.findByRole("status")).toHaveTextContent(GONE);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/admin/users").length).toBeGreaterThan(1));
  });

  it("closes the edit dialog on a 404 too", async () => {
    mockApi({
      [LIST]: page([tara, rahul]),
      [`PATCH /api/v1/admin/users/${rahul.id}`]: apiError(404, "not_found", "Not found."),
    });
    renderAs(teamAdminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Rahul Sharma" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    const dialog = screen.getByRole("dialog", { name: "Edit Rahul Sharma" });
    await user.type(within(dialog).getByLabelText("First name"), " K");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("status")).toHaveTextContent(GONE);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("other refusals still show in place (a 422 is not out of reach)", async () => {
    mockApi({
      [LIST]: page([tara, rahul]),
      [`POST /api/v1/admin/users/${rahul.id}/deactivate`]: apiError(422, "business_rule_violation", "Rahul Sharma can't be deactivated now."),
    });
    renderAs(teamAdminViewer);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Rahul Sharma" }));
    await user.click(screen.getByRole("menuitem", { name: "Deactivate" }));
    const confirm = screen.getByRole("alertdialog", { name: "Deactivate Rahul Sharma?" });
    await user.click(within(confirm).getByRole("button", { name: "Deactivate" }));
    expect(await within(confirm).findByRole("alert")).toHaveTextContent("Rahul Sharma can't be deactivated now.");
    expect(screen.queryByText(/no longer in your reach/)).not.toBeInTheDocument();
  });
});
