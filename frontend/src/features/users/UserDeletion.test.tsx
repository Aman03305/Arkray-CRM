/**
 * Deleting an account from the Users page (ADR-0036): its work is handed to someone else, it is
 * deactivated and its identity erased, and it stays in the list as a "Former user" (status
 * Deleted). A Super admin may delete Users and Admins, an Admin the Users of their team, nobody a
 * Super admin's account, their own or a deleted one. The UI only decides what to show and walks
 * the administrator through it: the API enforces all of it (backend/tests/security/test_account_deletion.py).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { registerStepUpHandler } from "@/lib/api/client";
import type { AdminUser, DeletionPreview } from "@/lib/api/types";
import type { Viewer } from "@/lib/viewer";
import { unnamedControls } from "@/test/a11y";
import { adminViewer, makeAdminUser, teamAdminViewer } from "@/test/fixtures";
import { apiError, mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

import { userDetailKey } from "./api";
import { UsersPage } from "./UsersPage";

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/users",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const LIST = "GET /api/v1/admin/users";
const ASSIGNEES = "GET /api/v1/assignees";

/** Someone with a name of their own; everything else is makeAdminUser's active User. */
function person(n: number, first: string, overrides: Partial<AdminUser> = {}): AdminUser {
  return makeAdminUser({
    id: `00000000-0000-4000-8000-00000000000${n}`,
    email: `${first.toLowerCase()}@example.test`,
    first_name: first,
    last_name: "Test",
    full_name: `${first} Test`,
    ...overrides,
  });
}
const INVITED = { status: "invited", status_label: "Invited", last_login: null, activated_at: null } as const;
const DEACTIVATED = { status: "deactivated", status_label: "Deactivated" } as const;
const ADMIN = { role: "team_admin", role_label: "Admin" } as const;

const rahul = makeAdminUser(); // an active User
const invitedUser = person(1, "Ivy", INVITED);
const deactivatedUser = person(2, "Dan", DEACTIVATED);
const admin = person(3, "Tara", ADMIN);
const invitedAdmin = person(4, "Omar", { ...ADMIN, ...INVITED });
const deactivatedAdmin = person(5, "Dev", { ...ADMIN, ...DEACTIVATED });
const superAdmin = person(6, "Bina", { role: "admin", role_label: "Super admin" });
const superSelf = person(7, "Anita", { id: adminViewer.id, role: "admin", role_label: "Super admin" });
const adminSelf = person(8, "Sam", { id: teamAdminViewer.id, ...ADMIN });
/** What the list shows of a deleted account: the pseudonym, deactivated for good. */
const former = (of: AdminUser, last: string): AdminUser => ({
  ...of,
  first_name: "Former user",
  last_name: last,
  full_name: `Former user ${last}`,
  email: `former-${last}@pseudonymised.invalid`,
  ...DEACTIVATED,
  pseudonymised_at: "2026-10-16T09:00:00Z",
  version: of.version + 1,
});
const formerRahul = former(rahul, "3f2b8c1e");

const priya = { id: "5c4b3a29-1807-4f6e-9d5c-4b3a29180716", full_name: "Priya Patel", email: "priya@example.test" };
const PRIYA_OPTION = "Priya Patel (priya@example.test)";

const path = (u: AdminUser, action = "") => `/api/v1/admin/users/${u.id}${action ? `/${action}` : ""}`;
const DELETE = (u: AdminUser) => `POST ${path(u, "delete")}`;
const PREVIEW = (u: AdminUser) => `GET ${path(u, "deletion-preview")}`;

/** What the server says deleting an account would do: by default, nothing to hand over. */
function preview(overrides: Partial<DeletionPreview> = {}): DeletionPreview {
  return {
    leads: 0,
    open_deals: 0,
    open_activities: 0,
    personal_pipelines: 0,
    team_members: 0,
    legal_hold: false,
    needs_new_owner: false,
    blocked_reason: null,
    ...overrides,
  };
}
const previewOf = (overrides: Partial<DeletionPreview> = {}) => ({ status: 200, body: preview(overrides) });
const OWNS_WORK = { leads: 12, open_deals: 3, open_activities: 5, needs_new_owner: true } as const;

/** The list and each user's details, the deletion preview (nothing to hand over) and the people
 * work can go to; `extra` adds or replaces routes. */
function serve(users: AdminUser[], extra: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    // A Super admin's page also loads the Admins for its Admin filter (role=team_admin).
    [LIST]: (call: RecordedCall) => ({
      status: 200,
      body: { results: call.query.get("role") === "team_admin" ? [] : users, next: null, previous: null },
    }),
    ...Object.fromEntries(users.map((u) => [`GET ${path(u)}`, { status: 200, body: u }])),
    ...Object.fromEntries(users.map((u) => [PREVIEW(u), previewOf()])),
    [ASSIGNEES]: {
      status: 200,
      body: {
        results: [
          { id: rahul.id, full_name: "Rahul Sharma", email: "rahul@example.test" },
          priya,
          { id: adminViewer.id, full_name: "Anita Admin", email: "admin@example.test" },
        ],
        next: null,
        previous: null,
      },
    },
    ...extra,
  });
}

const listCalls = (api: ReturnType<typeof mockApi>) =>
  api.callsTo("GET", "/api/v1/admin/users").filter((c) => c.query.get("role") !== "team_admin");

function renderAs(viewer: Viewer) {
  return renderWithProviders(<UsersPage />, { viewer });
}

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

async function openActions(name: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: `Actions for ${name}` }));
  return user;
}

async function openDetails(name: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name }));
  return { user, drawer: await screen.findByRole("dialog", { name }) };
}

async function openDelete(name = "Rahul Sharma") {
  const user = await openActions(name);
  await user.click(screen.getByRole("menuitem", { name: "Delete account" }));
  return { user, dialog: screen.getByRole("alertdialog", { name: `Delete ${name}?` }) };
}

type UserEvents = ReturnType<typeof userEvent.setup>;

/** The loaded dialog's controls. */
const emailField = (dialog: HTMLElement) => within(dialog).findByRole("textbox", { name: "Type their email to confirm" });
const deleteButton = (dialog: HTMLElement) => within(dialog).getByRole("button", { name: "Delete account" });

async function typeEmail(user: UserEvents, dialog: HTMLElement, email = "rahul@example.test") {
  await user.type(await emailField(dialog), email);
}

async function chooseOwner(user: UserEvents, dialog: HTMLElement) {
  const select = await within(dialog).findByRole("combobox", { name: "Hand their work to" });
  await within(select).findByRole("option", { name: PRIYA_OPTION });
  await user.selectOptions(select, PRIYA_OPTION);
}

const itemsOf = (dialog: HTMLElement) => within(dialog).getAllByRole("listitem").map((item) => item.textContent);
const optionLabels = (select: HTMLElement) => within(select).getAllByRole("option").map((o) => o.textContent);

const offers = (label: string) => screen.queryByRole("menuitem", { name: label }) !== null;

describe("who is offered Delete account", () => {
  it.each([
    ["an active User", rahul, true],
    ["an invited User", invitedUser, true],
    ["a deactivated User", deactivatedUser, true],
    ["an active Admin", admin, true],
    ["an invited Admin", invitedAdmin, true],
    ["a deactivated Admin", deactivatedAdmin, true],
    ["another Super admin (a Super admin leaves by deactivation)", superAdmin, false],
    ["the Super admin themselves", superSelf, false],
  ] as const)("a Super admin's menu, for %s: %s", async (_who, target, offered) => {
    serve([target]);
    renderAs(adminViewer);
    await openActions(target.full_name);
    expect(offers("Delete account")).toBe(offered);
  });

  it.each([
    ["an active User of their team", rahul, true],
    ["an invited User", invitedUser, true],
    ["a deactivated User", deactivatedUser, true],
    // The API lists an Admin only themselves and their team; if one ever came through, never an administrator.
    ["an Admin", admin, false],
    ["a Super admin", superAdmin, false],
    ["the Admin themselves", adminSelf, false],
  ] as const)("an Admin's menu, for %s: %s", async (_who, target, offered) => {
    serve([target]);
    renderAs(teamAdminViewer);
    await openActions(target.full_name);
    expect(offers("Delete account")).toBe(offered);
  });

  it("is last in the menu, after Deactivate, and as red as Deactivate", async () => {
    serve([rahul]);
    renderAs(adminViewer);
    await openActions("Rahul Sharma");
    const items = screen.getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "Edit details",
      "Change email",
      "Reset password",
      "Deactivate",
      "Delete account",
    ]);
    const red = (name: string) => screen.getByRole("menuitem", { name }).className.includes("text-red-700");
    expect(red("Delete account")).toBe(true);
    expect(red("Deactivate")).toBe(true);
    expect(red("Reset password")).toBe(false);
  });

  it("a deleted account is listed as a Former user with the status Deleted and no actions", async () => {
    serve([formerRahul]);
    renderAs(adminViewer);
    const row = (await screen.findByRole("button", { name: "Former user 3f2b8c1e" })).closest("tr")!;
    expect(within(row).getByText("Deleted")).toBeInTheDocument();
    expect(within(row).queryByText("Deactivated")).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /^Actions for/ })).not.toBeInTheDocument();
  });

  it("the details panel of a Former user says Deleted and offers nothing to do to the account", async () => {
    serve([formerRahul]);
    renderAs(adminViewer);
    const { drawer } = await openDetails("Former user 3f2b8c1e");
    expect(within(drawer).getByText("Status").nextElementSibling).toHaveTextContent("Deleted");
    expect(within(drawer).queryByRole("button", { name: "Delete account" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Reset password" })).not.toBeInTheDocument();
    expect(drawer).toHaveTextContent("This former user was pseudonymised: their account can't be changed.");
  });

  it.each([
    ["a User", rahul, true],
    ["an Admin", admin, true],
    ["a Super admin", superAdmin, false],
    ["the Super admin themselves", superSelf, false],
  ] as const)("the details panel of %s: %s", async (_who, target, offered) => {
    serve([target]);
    renderAs(adminViewer);
    const { drawer } = await openDetails(target.full_name);
    expect(within(drawer).queryByRole("button", { name: "Delete account" }) !== null).toBe(offered);
    if (offered) expect(drawer).toHaveTextContent("This can't be undone.");
  });

  it("an Admin's details panel offers it for a user of their team only", async () => {
    serve([adminSelf, rahul]);
    renderAs(teamAdminViewer);
    let { user, drawer } = await openDetails("Rahul Sharma");
    expect(within(drawer).getByRole("button", { name: "Delete account" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    ({ user, drawer } = await openDetails("Sam Test"));
    expect(within(drawer).queryByRole("button", { name: "Delete account" })).not.toBeInTheDocument();
  });

  it("marks Delete account in the panel with a red icon, like the other destructive entry points (the confirmation is the red button)", async () => {
    serve([rahul]);
    renderAs(adminViewer);
    const { drawer } = await openDetails("Rahul Sharma");
    const button = within(drawer).getByRole("button", { name: "Delete account" });
    expect(button.querySelector("svg")).toHaveClass("text-red-600");
  });
});

describe("the confirmation", () => {
  it("checks what the account owns first: a loading state, then the plain-language consequences", async () => {
    const gate = deferred<{ status: number; body: unknown }>();
    serve([rahul], { [PREVIEW(rahul)]: () => gate.promise });
    renderAs(adminViewer);
    const { dialog } = await openDelete();
    expect(within(dialog).getByText("Checking what this account owns…")).toBeInTheDocument();
    expect(within(dialog).getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(deleteButton(dialog)).toBeDisabled();
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();

    gate.resolve({ status: 200, body: preview({ ...OWNS_WORK, personal_pipelines: 2 }) });
    await within(dialog).findByText("Deleting this account will:");
    expect(within(dialog).queryByText("Checking what this account owns…")).not.toBeInTheDocument();
    expect(itemsOf(dialog)).toEqual([
      "hand over their 12 leads, 3 open deals and 5 open tasks and meetings to the person you choose below",
      "archive those of their 2 personal pipelines that hold no open deals (none is deleted)",
      "deactivate the account, sign them out everywhere and erase their name, email and other personal details",
      'keep CRM records and the audit trail, which show them as a "Former user"',
    ]);
  });

  it("says whose account it is, and that it can't be undone", async () => {
    serve([rahul], { [PREVIEW(rahul)]: previewOf(OWNS_WORK) });
    renderAs(adminViewer);
    const { dialog } = await openDelete();
    expect(within(dialog).getByText("Rahul Sharma")).toBeInTheDocument();
    expect(within(dialog).getByText("User")).toBeInTheDocument();
    expect(within(dialog).getAllByText("rahul@example.test").length).toBeGreaterThan(0);
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("This can't be undone."));
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("personal details are erased"));
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("CRM records and the audit trail are kept"));
  });

  it("reads the counts in the singular", async () => {
    serve([rahul], {
      [PREVIEW(rahul)]: previewOf({ leads: 1, open_deals: 1, open_activities: 1, personal_pipelines: 1, needs_new_owner: true }),
    });
    renderAs(adminViewer);
    const { dialog } = await openDelete();
    await within(dialog).findByText("Deleting this account will:");
    expect(itemsOf(dialog)[0]).toBe("hand over their 1 lead, 1 open deal and 1 open task or meeting to the person you choose below");
    expect(itemsOf(dialog)[1]).toBe("archive their personal pipeline if it holds no open deals (it is never deleted)");
  });

  it("groups big numbers the Indian way", async () => {
    serve([rahul], { [PREVIEW(rahul)]: previewOf({ leads: 123456, needs_new_owner: true }) });
    renderAs(adminViewer);
    const { dialog } = await openDelete();
    await within(dialog).findByText("Deleting this account will:");
    expect(itemsOf(dialog)[0]).toBe("hand over their 1,23,456 leads to the person you choose below");
  });

  it("tells what happens to an Admin's team and to what they set up", async () => {
    serve([admin], { [PREVIEW(admin)]: previewOf({ team_members: 4 }) });
    renderAs(adminViewer);
    const { dialog } = await openDelete("Tara Test");
    await within(dialog).findByText("Deleting this account will:");
    expect(within(dialog).getByText("Admin")).toBeInTheDocument();
    expect(itemsOf(dialog)).toEqual([
      "release their team of 4 users, who have no Admin until a Super admin assigns one",
      "cancel the invitations they sent and end the temporary passwords they set",
      "deactivate the account, sign them out everywhere and erase their name, email and other personal details",
      'keep CRM records and the audit trail, which show them as a "Former user"',
    ]);
  });

  it("says so when there is nothing to hand over, and asks for no new owner", async () => {
    serve([rahul]);
    renderAs(adminViewer);
    const { dialog } = await openDelete();
    await within(dialog).findByText("Deleting this account will:");
    expect(dialog).toHaveTextContent("They don't own any leads, deals, tasks or meetings, so nothing needs handing over.");
    expect(within(dialog).queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("enables the destructive button only once their email is typed in full", async () => {
    const api = serve([rahul]);
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    const field = await emailField(dialog);
    const button = deleteButton(dialog);
    expect(field).toHaveAccessibleDescription("rahul@example.test"); // what to type
    expect(button).toBeDisabled();
    await user.type(field, "rahul@example.tes");
    expect(button).toBeDisabled();
    await user.type(field, "t");
    expect(button).toBeEnabled();
    // The address is the same whatever its case or the spaces around it.
    await user.clear(field);
    await user.type(field, "  RAHUL@Example.TEST ");
    expect(button).toBeEnabled();
    await user.clear(field);
    expect(button).toBeDisabled();
    // Enter can't submit a disabled form either.
    await user.type(field, "rahul@elsewhere.test{Enter}");
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(0);
  });

  it("asks who takes over their work, never offering the account itself, and requires the choice", async () => {
    const api = serve([rahul], {
      [PREVIEW(rahul)]: previewOf(OWNS_WORK),
      [DELETE(rahul)]: { status: 200, body: { completed: true, reassigned_leads: 12, remaining_leads: 0 } },
    });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    const select = await within(dialog).findByRole("combobox", { name: "Hand their work to" });
    await within(select).findByRole("option", { name: PRIYA_OPTION });
    // The people work can go to, minus the account being deleted.
    expect(optionLabels(select)).toEqual(["Choose who takes over", PRIYA_OPTION, "Anita Admin (admin@example.test)"]);
    expect(select).toHaveValue("");
    expect(select).toHaveAccessibleDescription(expect.stringContaining("They become the owner of those leads"));

    await typeEmail(user, dialog);
    expect(deleteButton(dialog)).toBeDisabled(); // the email isn't enough
    await user.selectOptions(select, PRIYA_OPTION);
    expect(deleteButton(dialog)).toBeEnabled();
    await user.click(deleteButton(dialog));
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "delete"))[0]!.body).toEqual({
      version: 1,
      confirm_email: "rahul@example.test",
      reassign_to: priya.id,
    });
  });

  it("sends no new owner when there is nothing to hand over", async () => {
    const api = serve([rahul], { [DELETE(rahul)]: { status: 200, body: { completed: true, reassigned_leads: 0, remaining_leads: 0 } } });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "delete"))[0]!.body).toEqual({ version: 1, confirm_email: "rahul@example.test" });
  });

  it.each([
    [
      "a legal hold",
      { legal_hold: true, blocked_reason: "The user 3f2b8c1e is under a legal hold: release it first (an administrator, with the matter's reference)." },
      "The user 3f2b8c1e is under a legal hold: release it first",
    ],
    [
      "a Super admin's account",
      { blocked_reason: "A super admin's account is managed only by that super admin: it can't be reset or deleted here." },
      "A super admin's account is managed only by that super admin",
    ],
  ] as const)("shows why it can't be deleted (%s) and keeps the button disabled", async (_why, blocked, shown) => {
    serve([rahul], { [PREVIEW(rahul)]: previewOf({ ...OWNS_WORK, ...blocked }) });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("This account can't be deleted");
    expect(alert).toHaveTextContent(shown);
    expect(deleteButton(dialog)).toBeDisabled();
    // Nothing to confirm, and nothing is promised: no consequences, no fields.
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("Deleting this account will:")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("explains a preview that couldn't be loaded, offers to try again, and deletes nothing meanwhile", async () => {
    let attempts = 0;
    const api = serve([rahul], {
      [PREVIEW(rahul)]: () => {
        attempts += 1;
        return attempts === 1 ? apiError(500, "server_error", "Boom") : previewOf();
      },
    });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("We couldn't check what deleting this account would do.");
    expect(alert).toHaveTextContent("Something went wrong on our side. Please try again in a moment.");
    expect(alert).toHaveTextContent("Reference: req-test-1");
    expect(alert).not.toHaveTextContent("Boom");
    expect(deleteButton(dialog)).toBeDisabled();
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    await within(dialog).findByText("Deleting this account will:");
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(0);
  });

  it("closes and says so when the account is out of reach now (404)", async () => {
    serve([rahul], { [PREVIEW(rahul)]: apiError(404, "not_found", "Not found.") });
    renderAs(adminViewer);
    // It may be gone again by the time anyone looks: the answer comes straight away.
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Delete account" }));
    expect(
      await screen.findByText(
        "Rahul Sharma is no longer in your reach (they may have moved to another team). Nothing was changed, and the list has been refreshed.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("reads what they own afresh every time it opens", async () => {
    let leads = 3;
    const api = serve([rahul], {
      [PREVIEW(rahul)]: () => ({ status: 200, body: preview({ leads, needs_new_owner: true }) }),
    });
    renderAs(adminViewer);
    let { user, dialog } = await openDelete();
    await within(dialog).findByText("hand over their 3 leads to the person you choose below");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    leads = 5; // work arrived meanwhile
    ({ user, dialog } = await openDelete());
    await within(dialog).findByText("hand over their 5 leads to the person you choose below");
    expect(api.callsTo("GET", path(rahul, "deletion-preview"))).toHaveLength(2);
  });

  it("Cancel and Escape close it without deleting; focus starts on the safe choice and comes back to the row", async () => {
    const api = serve([rahul]);
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actions for Rahul Sharma" })).toHaveFocus();
    const again = await openDelete();
    await typeEmail(again.user, again.dialog);
    await again.user.click(within(again.dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(0);
  });
});

describe("deleting", () => {
  it("deletes from the row's menu: closes, says so, and lists the account as a Former user", async () => {
    let deleted = false;
    const api = serve([rahul], {
      [LIST]: (call: RecordedCall) => ({
        status: 200,
        body: { results: call.query.get("role") === "team_admin" ? [] : [deleted ? formerRahul : rahul], next: null, previous: null },
      }),
      [DELETE(rahul)]: () => {
        deleted = true;
        return { status: 200, body: { completed: true, reassigned_leads: 0, remaining_leads: 0 } };
      },
    });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));

    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(1);
    // The list is read again and now shows the pseudonym, Deleted, without actions.
    const row = (await screen.findByRole("button", { name: "Former user 3f2b8c1e" })).closest("tr")!;
    expect(within(row).getByText("Deleted")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /^Actions for/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rahul Sharma" })).not.toBeInTheDocument();
    expect(listCalls(api).length).toBeGreaterThan(1);
  });

  it("deletes from the details panel: the panel closes with it, and no stale copy of the account stays cached", async () => {
    const api = serve([rahul], { [DELETE(rahul)]: { status: 200, body: { completed: true, reassigned_leads: 0, remaining_leads: 0 } } });
    const { client } = renderAs(adminViewer);
    const { user, drawer } = await openDetails("Rahul Sharma");
    await waitFor(() => expect(client.getQueryData(userDetailKey(rahul.id))).toBeDefined());
    await user.click(within(drawer).getByRole("button", { name: "Delete account" }));
    expect(screen.queryByRole("dialog", { name: "Rahul Sharma" })).not.toBeInTheDocument(); // no stacked modals
    const dialog = screen.getByRole("alertdialog", { name: "Delete Rahul Sharma?" });
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));

    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(client.getQueryData(userDetailKey(rahul.id))).toBeUndefined();
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(1);
  });

  it.each([
    ["the row's menu", false],
    ["the details panel", true],
  ] as const)("moves focus to the page's heading afterwards: nothing is left of what had it (%s)", async (_from, viaPanel) => {
    serve([rahul], { [DELETE(rahul)]: { status: 200, body: { completed: true, reassigned_leads: 0, remaining_leads: 0 } } });
    renderWithProviders(
      <main>
        <UsersPage />
      </main>,
      { viewer: adminViewer },
    );
    let dialog: HTMLElement;
    let user: UserEvents;
    if (viaPanel) {
      const panel = await openDetails("Rahul Sharma");
      await panel.user.click(within(panel.drawer).getByRole("button", { name: "Delete account" }));
      user = panel.user;
      dialog = screen.getByRole("alertdialog", { name: "Delete Rahul Sharma?" });
    } else {
      ({ user, dialog } = await openDelete());
    }
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    await screen.findByText("Rahul Sharma was deleted.");
    await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "Users" })).toHaveFocus());
  });

  it("cancelling from the details panel brings the panel back", async () => {
    serve([rahul]);
    renderAs(adminViewer);
    const { user, drawer } = await openDetails("Rahul Sharma");
    await user.click(within(drawer).getByRole("button", { name: "Delete account" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Rahul Sharma" })).toBeInTheDocument();
  });

  it("refreshes what other pages cached: the people work can go to, and the CRM only when work was handed over", async () => {
    serve([rahul], {
      [PREVIEW(rahul)]: previewOf({ leads: 3, needs_new_owner: true }),
      [DELETE(rahul)]: { status: 200, body: { completed: true, reassigned_leads: 3, remaining_leads: 0 } },
    });
    const { client } = renderAs(adminViewer);
    // Other pages' cached data, none of it in use here (the picker and the filters use their own keys).
    const keys = [
      ["assignees", "zz"],
      ["pipeline", "board", "me"],
      ["activities", "list", "me"],
      ["leads", "detail", "me", "x"],
      ["admin-users", "detail", "someone-else"],
    ] as const;
    for (const key of keys) client.setQueryData(key, { cached: true });
    const { user, dialog } = await openDelete();
    await chooseOwner(user, dialog);
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    await screen.findByText("Rahul Sharma was deleted.");
    for (const key of keys) expect(client.getQueryState(key)?.isInvalidated, key.join("/")).toBe(true);
  });

  it("leaves the CRM's cached pages alone when nothing was handed over", async () => {
    serve([rahul], { [DELETE(rahul)]: { status: 200, body: { completed: true, reassigned_leads: 0, remaining_leads: 0 } } });
    const { client } = renderAs(adminViewer);
    for (const key of [["pipeline", "board", "me"], ["activities", "list", "me"], ["leads", "detail", "me", "x"]] as const) {
      client.setQueryData(key, { cached: true });
    }
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    await screen.findByText("Rahul Sharma was deleted.");
    expect(client.getQueryState(["pipeline", "board", "me"])?.isInvalidated).toBe(false);
    expect(client.getQueryState(["activities", "list", "me"])?.isInvalidated).toBe(false);
    expect(client.getQueryState(["leads", "detail", "me", "x"])?.isInvalidated).toBe(false);
  });

  it("an Admin deletes a user of their team", async () => {
    const api = serve([adminSelf, rahul], {
      [PREVIEW(rahul)]: previewOf({ leads: 2, needs_new_owner: true }),
      [DELETE(rahul)]: { status: 200, body: { completed: true, reassigned_leads: 2, remaining_leads: 0 } },
    });
    renderAs(teamAdminViewer);
    const { user, dialog } = await openDelete();
    await chooseOwner(user, dialog);
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "delete"))[0]!.body).toMatchObject({ reassign_to: priya.id });
  });

  it("shows the button busy while it works, and can't be dismissed or sent twice", async () => {
    const gate = deferred<{ status: number; body: unknown }>();
    const api = serve([rahul], { [DELETE(rahul)]: () => gate.promise });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    const button = deleteButton(dialog);
    await user.click(button);
    await waitFor(() => expect(button).toHaveAttribute("aria-busy", "true"));
    await user.click(button);
    await user.keyboard("{Escape}");
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Close dialog" })).toBeDisabled();
    expect(within(dialog).getByRole("textbox", { name: "Type their email to confirm" })).toHaveAttribute("readonly");
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(1);

    gate.resolve({ status: 200, body: { completed: true, reassigned_leads: 0, remaining_leads: 0 } });
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(1);
  });

  it("asks for a recent second step when the API wants one, then deletes", async () => {
    let verified = false;
    const handler = vi.fn(async () => {
      verified = true;
      return true;
    });
    const unregister = registerStepUpHandler(handler);
    try {
      const api = serve([rahul], {
        [DELETE(rahul)]: () =>
          verified
            ? { status: 200, body: { completed: true, reassigned_leads: 0, remaining_leads: 0 } }
            : apiError(403, "mfa_required", "Confirm it's you first."),
      });
      renderAs(adminViewer);
      const { user, dialog } = await openDelete();
      await typeEmail(user, dialog);
      await user.click(deleteButton(dialog));
      expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
      expect(handler).toHaveBeenCalledTimes(1);
      expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(2);
    } finally {
      unregister();
    }
  });
});

describe("an account with many leads is handed over in batches", () => {
  const STEPS = [
    { status: 202, body: { completed: false, reassigned_leads: 200, remaining_leads: 300 } },
    { status: 202, body: { completed: false, reassigned_leads: 200, remaining_leads: 100 } },
    { status: 200, body: { completed: true, reassigned_leads: 100, remaining_leads: 0 } },
  ];

  /** The delete route answering STEPS one by one, each only once its gate is opened. */
  function batches(steps: { status: number; body?: unknown; headers?: Record<string, string> }[] = STEPS) {
    const gates = steps.map(() => deferred());
    let sent = 0;
    const route = async () => {
      const i = sent++;
      await gates[i]?.promise;
      return steps[i] ?? steps[steps.length - 1]!;
    };
    return { gates, route };
  }

  async function startDeleting(extra: Parameters<typeof mockApi>[0]) {
    const api = serve([rahul], { [PREVIEW(rahul)]: previewOf({ leads: 500, needs_new_owner: true }), ...extra });
    const view = renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await chooseOwner(user, dialog);
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    return { api, user, dialog, view };
  }

  const sent = (api: ReturnType<typeof mockApi>) => api.callsTo("POST", path(rahul, "delete"));

  it("sends the same request again after every 202 until the 200, showing how far it is", async () => {
    const { gates, route } = batches();
    const { api, dialog } = await startDeleting({ [DELETE(rahul)]: route });
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    expect(within(dialog).queryByText(/Handed over/)).not.toBeInTheDocument(); // nothing handed over yet

    gates[0]!.resolve();
    expect(await within(dialog).findByText("Handed over 200 leads, 300 to go…")).toBeInTheDocument();
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument(); // stays open meanwhile
    expect(deleteButton(dialog)).toHaveAttribute("aria-busy", "true");

    gates[1]!.resolve();
    expect(await within(dialog).findByText("Handed over 400 leads, 100 to go…")).toBeInTheDocument();
    await waitFor(() => expect(sent(api)).toHaveLength(3));

    gates[2]!.resolve();
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    // Three times the very same request: the server tells the batches apart, not the client.
    const bodies = sent(api).map((call) => call.body);
    expect(bodies).toHaveLength(3);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[2]).toEqual(bodies[0]);
    expect(bodies[0]).toEqual({ version: 1, confirm_email: "rahul@example.test", reassign_to: priya.id });
  });

  it("announces the progress politely, in one status region", async () => {
    const { gates, route } = batches();
    const { dialog } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    const status = await within(dialog).findByText("Handed over 200 leads, 300 to go…");
    expect(status.closest('[role="status"]')).toBeInTheDocument();
    gates[1]!.resolve();
    await within(dialog).findByText("Handed over 400 leads, 100 to go…");
    expect(within(dialog).getAllByRole("status")).toHaveLength(1);
    gates[2]!.resolve();
    await screen.findByText("Rahul Sharma was deleted.");
  });

  it("can be stopped between two batches: what was handed over stays, the account stays, and it can go on", async () => {
    const { gates, route } = batches();
    const { api, user, dialog } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    await within(dialog).findByText("Handed over 200 leads, 300 to go…");
    // While it runs, the way out is Stop (not Cancel); the dialog can't be dismissed.
    expect(within(dialog).queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));
    expect(within(dialog).getByRole("button", { name: "Stopping…" })).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Stopping after this batch.");

    gates[1]!.resolve(); // the batch in flight finishes, then it stops
    expect(await within(dialog).findByText("Stopped before finishing")).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      "400 leads were handed over, 100 to go. The account hasn't been deleted: delete it again to carry on.",
    );
    expect(sent(api)).toHaveLength(2);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Close" })).toBeEnabled();
    expect(within(dialog).queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
    // Focus did not leave with the Stop button that had it.
    expect(within(dialog).getByRole("button", { name: "Close" })).toHaveFocus();

    // Carrying on repeats the same request (what the administrator typed and chose is still there).
    expect(deleteButton(dialog)).toBeEnabled();
    await user.click(deleteButton(dialog));
    await waitFor(() => expect(sent(api)).toHaveLength(3));
    gates[2]!.resolve();
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(sent(api)[2]!.body).toEqual(sent(api)[0]!.body);
  });

  it("reads what is left again after a stop, so the list of consequences is true when it goes on", async () => {
    let leads = 500;
    const { gates, route } = batches();
    const { api, user, dialog } = await startDeleting({
      [PREVIEW(rahul)]: () => ({ status: 200, body: preview({ leads, needs_new_owner: true }) }),
      [DELETE(rahul)]: route,
    });
    expect(itemsOf(dialog)[0]).toBe("hand over their 500 leads to the person you choose below");
    gates[0]!.resolve();
    await within(dialog).findByText("Handed over 200 leads, 300 to go…");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));
    leads = 100; // what the next look at the account finds
    gates[1]!.resolve();
    await within(dialog).findByText("Stopped before finishing");
    await within(dialog).findByText("hand over their 100 leads to the person you choose below");
    expect(api.callsTo("GET", path(rahul, "deletion-preview"))).toHaveLength(2);
  });

  it("stopping and closing leaves the account active, without another request", async () => {
    const { gates, route } = batches();
    const { api, user, dialog } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    await within(dialog).findByText("Handed over 200 leads, 300 to go…");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));
    gates[1]!.resolve();
    await within(dialog).findByText("Stopped before finishing");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Rahul Sharma was deleted.")).not.toBeInTheDocument();
    expect(sent(api)).toHaveLength(2);
  });

  it("a delete that finishes while Stop was pressed is simply done", async () => {
    const { gates, route } = batches([STEPS[0]!, STEPS[2]!]);
    const { api, user, dialog } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    await within(dialog).findByText("Handed over 200 leads, 300 to go…");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));
    gates[1]!.resolve(); // the request in flight was the last one
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(sent(api)).toHaveLength(2);
  });

  it("leaving the page ends it after the batch in flight", async () => {
    const { gates, route } = batches();
    const { api, view } = await startDeleting({ [DELETE(rahul)]: route });
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    view.unmount();
    gates[0]!.resolve();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sent(api)).toHaveLength(1);
  });

  it("gives up when the server says there is more but hands over nothing, instead of repeating forever", async () => {
    const { gates, route } = batches([{ status: 202, body: { completed: false, reassigned_leads: 0, remaining_leads: 5 } }]);
    const { api, dialog } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("The server didn't hand over any more leads, so this stopped. The account is still active.");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sent(api)).toHaveLength(3); // three rounds in a row that move nothing
    expect(deleteButton(dialog)).toBeEnabled(); // it can be tried again
  });

  it("repeats after a round that hands over nothing when the next one moves something", async () => {
    // A lead arrived between the server's check and its final step: 202 with nothing moved, then done.
    const { gates, route } = batches([
      { status: 202, body: { completed: false, reassigned_leads: 0, remaining_leads: 1 } },
      { status: 200, body: { completed: true, reassigned_leads: 1, remaining_leads: 0 } },
    ]);
    const { api } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    gates[1]!.resolve();
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(sent(api)).toHaveLength(2);
  });

  it("gives up on a 202 that carries no count as well", async () => {
    const { gates, route } = batches([{ status: 202, body: {} }]);
    const { api, dialog } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The server didn't hand over any more leads, so this stopped.");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sent(api)).toHaveLength(3);
    expect(dialog).not.toHaveTextContent("NaN");
  });

  it("says how many leads were already handed over when a later batch fails", async () => {
    const { gates, route } = batches([
      STEPS[0]!,
      { ...apiError(409, "conflict", "The record was changed by someone else. Reload and try again."), status: 409 },
    ]);
    const { api, dialog } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    gates[1]!.resolve();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Someone else changed this account meanwhile.");
    expect(alert).toHaveTextContent("200 leads were already handed over; the account is still active.");
    expect(sent(api)).toHaveLength(2);
    await waitFor(() => expect(listCalls(api).length).toBeGreaterThan(1));
  });

  it("says '1 lead was' for a single lead", async () => {
    const { gates, route } = batches([
      { status: 202, body: { completed: false, reassigned_leads: 1, remaining_leads: 1 } },
      apiError(500, "server_error", "Boom"),
    ]);
    const { dialog } = await startDeleting({ [DELETE(rahul)]: route });
    gates[0]!.resolve();
    gates[1]!.resolve();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("1 lead was already handed over; the account is still active.");
  });
});

describe("when deleting is refused", () => {
  const REFUSALS = [
    [
      "someone changed the account (409)",
      apiError(409, "conflict", "The record was changed by someone else. Reload and try again."),
      "Someone else changed this account meanwhile. The list has been refreshed: close this dialog and try again.",
      true,
    ],
    [
      "a legal hold (409)",
      apiError(409, "legal_hold", "The user 3f2b8c1e is under a legal hold: release it first (an administrator, with the matter's reference)."),
      "The user 3f2b8c1e is under a legal hold: release it first (an administrator, with the matter's reference).",
      true,
    ],
    [
      "a Super admin's account (403)",
      apiError(403, "permission_denied", "A super admin's account is managed only by that super admin: it can't be reset or deleted here."),
      "A super admin's account is managed only by that super admin: it can't be reset or deleted here.",
      false,
    ],
    [
      "a wrong confirmation (422)",
      apiError(422, "business_rule_violation", "Type the account's email address to confirm."),
      "Type the account's email address to confirm.",
      true,
    ],
    [
      "an account deleted by someone else meanwhile (422)",
      apiError(422, "account_pseudonymised", "This account was already pseudonymised."),
      "This account was already pseudonymised.",
      true,
    ],
    [
      "too many requests (429)",
      { ...apiError(429, "rate_limited", "Request was throttled. Expected available in 37 seconds."), headers: { "Retry-After": "37" } },
      "Too many requests. Try again in about a minute.",
      false,
    ],
    ["a server error (500)", apiError(500, "server_error", "Boom"), "Something went wrong on our side. Please try again in a moment.", false],
    [
      "no second step (403 mfa_required)",
      apiError(403, "mfa_required", "Confirm it's you first."),
      "Confirm it's you with a code from your authenticator app to do that.",
      false,
    ],
  ] as const;

  it.each(REFUSALS)("explains %s", async (_what, reply, shown, refreshes) => {
    const api = serve([rahul], { [DELETE(rahul)]: reply });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(shown);
    expect(alert).not.toHaveTextContent("Request was throttled");
    expect(alert).not.toHaveTextContent("Boom");
    // It stays open with the button ready again, so it can be retried or cancelled.
    expect(deleteButton(dialog)).toBeEnabled();
    expect(deleteButton(dialog)).not.toHaveAttribute("aria-busy");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(1);
    if (refreshes) await waitFor(() => expect(listCalls(api).length).toBeGreaterThan(1));
    else expect(listCalls(api)).toHaveLength(1);
  });

  it("turns into the blocked one when the retry meets a legal hold placed meanwhile, saying so once", async () => {
    const HOLD = "The user 3f2b8c1e is under a legal hold: release it first (an administrator, with the matter's reference).";
    let held = false;
    const api = serve([rahul], {
      [PREVIEW(rahul)]: () => ({ status: 200, body: preview(held ? { legal_hold: true, blocked_reason: HOLD } : {}) }),
      [DELETE(rahul)]: () => {
        held = true;
        return apiError(409, "legal_hold", HOLD);
      },
    });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));

    // The account is read again and now says why; the email typed before doesn't make it deletable.
    await within(dialog).findByText("This account can't be deleted");
    expect(within(dialog).getAllByRole("alert")).toHaveLength(1);
    expect(dialog).toHaveTextContent(HOLD);
    expect(deleteButton(dialog)).toBeDisabled();
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(1);
  });

  it("quotes the reference of a server error", async () => {
    serve([rahul], { [DELETE(rahul)]: apiError(500, "server_error", "Boom") });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Reference: req-test-1");
  });

  it("shows the server's wish for a new owner on the field, even if the preview saw no work", async () => {
    let wants = true;
    const api = serve([rahul], {
      [DELETE(rahul)]: () =>
        wants
          ? apiError(400, "validation_error", "Some fields are invalid.", { reassign_to: ["Choose who takes over this user's work."] })
          : { status: 200, body: { completed: true, reassigned_leads: 2, remaining_leads: 0 } },
    });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));

    const select = await within(dialog).findByRole("combobox", { name: "Hand their work to" });
    expect(select).toHaveAccessibleDescription(expect.stringContaining("Choose who takes over this user's work."));
    expect(select).toHaveAttribute("aria-invalid", "true");
    expect(select).toHaveFocus();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument(); // on the field it is about
    expect(deleteButton(dialog)).toBeDisabled(); // until someone is chosen

    wants = false;
    await chooseOwner(user, dialog);
    await user.click(deleteButton(dialog));
    expect(await screen.findByText("Rahul Sharma was deleted.")).toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "delete"))[1]!.body).toEqual({
      version: 1,
      confirm_email: "rahul@example.test",
      reassign_to: priya.id,
    });
  });

  it("shows a refused new owner on the picker and lets another be chosen", async () => {
    const api = serve([rahul], {
      [PREVIEW(rahul)]: previewOf(OWNS_WORK),
      [DELETE(rahul)]: apiError(400, "validation_error", "Some fields are invalid.", {
        reassign_to: ["Choose an active user who can take over this work."],
      }),
    });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await chooseOwner(user, dialog);
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    const select = await within(dialog).findByRole("combobox", { name: "Hand their work to" });
    await waitFor(() => expect(select).toHaveAccessibleDescription(expect.stringContaining("Choose an active user who can take over this work.")));
    expect(select).toHaveValue(priya.id);
    expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(1);
  });

  it("closes and says so when the account is out of reach now (404)", async () => {
    const api = serve([rahul], { [DELETE(rahul)]: apiError(404, "not_found", "Not found.") });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await typeEmail(user, dialog);
    await user.click(deleteButton(dialog));
    expect(await screen.findByText(/Rahul Sharma is no longer in your reach/)).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(listCalls(api).length).toBeGreaterThan(1));
  });

  it("says so when the second step is cancelled, and nothing was deleted", async () => {
    const unregister = registerStepUpHandler(async () => false);
    try {
      const api = serve([rahul], { [DELETE(rahul)]: apiError(403, "mfa_required", "Confirm it's you first.") });
      renderAs(adminViewer);
      const { user, dialog } = await openDelete();
      await typeEmail(user, dialog);
      await user.click(deleteButton(dialog));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Confirm it's you with a code from your authenticator app");
      expect(api.callsTo("POST", path(rahul, "delete"))).toHaveLength(1);
    } finally {
      unregister();
    }
  });
});

describe("the Admin filter", () => {
  it("does not offer a deleted Admin, who has no team left to filter by", async () => {
    const formerAdmin = former(admin, "9d8c7b6a");
    mockApi({
      [LIST]: (call: RecordedCall) => ({
        status: 200,
        body: { results: call.query.get("role") === "team_admin" ? [formerAdmin, admin] : [rahul], next: null, previous: null },
      }),
    });
    renderAs(adminViewer);
    await screen.findByRole("button", { name: "Rahul Sharma" });
    const filter = screen.getByLabelText("Filter by Admin");
    await waitFor(() => expect(optionLabels(filter)).toContain("Tara Test"));
    expect(optionLabels(filter)).toEqual(["Any Admin", "No admin", "Tara Test"]);
  });
});

describe("accessibility and phones", () => {
  it("names the dialog, its fields and every control; the page behind has nothing unnamed either", async () => {
    serve([rahul], { [PREVIEW(rahul)]: previewOf(OWNS_WORK) });
    renderAs(adminViewer);
    const { dialog } = await openDelete();
    await within(dialog).findByRole("combobox", { name: "Hand their work to" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByRole("textbox", { name: "Type their email to confirm" })).toBeInTheDocument();
    expect(within(dialog).getAllByRole("button").map((b) => b.getAttribute("aria-label") ?? b.textContent)).toEqual([
      "Close dialog",
      "Cancel",
      "Delete account",
    ]);
    expect(unnamedControls(document.body)).toEqual([]);
  });

  it("keeps Tab inside the dialog and Escape closes it", async () => {
    serve([rahul], { [PREVIEW(rahul)]: previewOf(OWNS_WORK) });
    renderAs(adminViewer);
    const { user, dialog } = await openDelete();
    await within(dialog).findByRole("combobox", { name: "Hand their work to" });
    for (let i = 0; i < 8; i++) {
      await user.tab();
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
    }
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("is made for a 320 px phone: it fills the width, long text wraps and the buttons stack", async () => {
    const long = person(1, "A".repeat(60), { email: `${"b".repeat(60)}@example.test`, full_name: `${"A".repeat(60)} Test` });
    serve([long], { [PREVIEW(long)]: previewOf(OWNS_WORK) });
    renderAs(adminViewer);
    const { dialog } = await openDelete(long.full_name);
    await within(dialog).findByRole("combobox", { name: "Hand their work to" });
    expect(dialog).toHaveClass("w-full");
    expect(dialog.parentElement).toHaveClass("overflow-y-auto", "p-4"); // scrolls, with a margin on a phone
    expect(within(dialog).getAllByText(long.email)[0]).toHaveClass("[overflow-wrap:anywhere]");
    expect(within(dialog).getAllByText(long.full_name)[0]).toHaveClass("[overflow-wrap:anywhere]");
    const buttons = within(dialog).getByRole("button", { name: "Cancel" }).parentElement!;
    expect(buttons).toHaveClass("flex-col-reverse", "sm:flex-row");
    // Nothing is a fixed width that could outgrow a narrow screen.
    expect(dialog.outerHTML).not.toMatch(/\b(?:min-)?w-\[\d+px\]|\bw-\d{2,}\b/);
  });
});
