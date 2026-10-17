/**
 * Having a password reset link emailed to an account, from the Users page (ADR-0036). The link
 * goes to the account's own address and the administrator never sees or chooses a password. A
 * Super admin may do it for Users and Admins, an Admin for the Users of their team, nobody for a
 * Super admin's account, their own, an account that isn't active or a deleted one. The UI only
 * decides what to show: the API enforces all of it (backend/tests/security/test_account_deletion.py).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { registerStepUpHandler } from "@/lib/api/client";
import type { AdminUser } from "@/lib/api/types";
import type { Viewer } from "@/lib/viewer";
import { unnamedControls } from "@/test/a11y";
import { adminViewer, makeAdminUser, teamAdminViewer } from "@/test/fixtures";
import { apiError, mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

import { UsersPage } from "./UsersPage";

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/users",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const LIST = "GET /api/v1/admin/users";

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
const former = person(9, "Former", {
  first_name: "Former user",
  last_name: "4c3b2a19",
  full_name: "Former user 4c3b2a19",
  ...DEACTIVATED,
  pseudonymised_at: "2026-10-02T09:00:00Z",
});

const path = (u: AdminUser, action = "") => `/api/v1/admin/users/${u.id}${action ? `/${action}` : ""}`;
const RESET = (u: AdminUser) => `POST ${path(u, "reset-password")}`;

/** The list and each user's details; `extra` adds or replaces routes. */
function serve(users: AdminUser[], extra: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    // A Super admin's page also loads the Admins for its Admin filter (role=team_admin).
    [LIST]: (call: RecordedCall) => ({
      status: 200,
      body: { results: call.query.get("role") === "team_admin" ? [] : users, next: null, previous: null },
    }),
    ...Object.fromEntries(users.map((u) => [`GET ${path(u)}`, { status: 200, body: u }])),
    ...extra,
  });
}

const listCalls = (api: ReturnType<typeof mockApi>) =>
  api.callsTo("GET", "/api/v1/admin/users").filter((c) => c.query.get("role") !== "team_admin");

function renderAs(viewer: Viewer) {
  return renderWithProviders(<UsersPage />, { viewer });
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

const offers = (label: string) => screen.queryByRole("menuitem", { name: label }) !== null;

describe("who is offered Reset password in a row's menu", () => {
  it.each([
    ["an active User", rahul, true],
    ["an active Admin", admin, true],
    ["an invited User (their invitation can be resent)", invitedUser, false],
    ["a deactivated User (reactivate them first)", deactivatedUser, false],
    ["an invited Admin", invitedAdmin, false],
    ["a deactivated Admin", deactivatedAdmin, false],
    ["another Super admin", superAdmin, false],
    ["the Super admin themselves (Settings)", superSelf, false],
  ] as const)("a Super admin, for %s: %s", async (_who, target, offered) => {
    serve([target]);
    renderAs(adminViewer);
    await openActions(target.full_name);
    expect(offers("Reset password")).toBe(offered);
  });

  it.each([
    ["an active User of their team", rahul, true],
    ["an invited User", invitedUser, false],
    ["a deactivated User", deactivatedUser, false],
    // The API lists an Admin only themselves and their team; if one ever came through, never an administrator.
    ["an Admin", admin, false],
    ["a Super admin", superAdmin, false],
    ["the Admin themselves (Settings)", adminSelf, false],
  ] as const)("an Admin, for %s: %s", async (_who, target, offered) => {
    serve([target]);
    renderAs(teamAdminViewer);
    await openActions(target.full_name);
    expect(offers("Reset password")).toBe(offered);
  });

  it("a deleted account has no menu at all", async () => {
    serve([former]);
    renderAs(adminViewer);
    await screen.findByRole("button", { name: "Former user 4c3b2a19" });
    expect(screen.queryByRole("button", { name: "Actions for Former user 4c3b2a19" })).not.toBeInTheDocument();
  });

  it("sits after Change email, before the lifecycle actions", async () => {
    serve([rahul]);
    renderAs(adminViewer);
    await openActions("Rahul Sharma");
    const labels = screen.getAllByRole("menuitem").map((item) => item.textContent);
    expect(labels.slice(0, 3)).toEqual(["Edit details", "Change email", "Reset password"]);
  });

  it("is not offered to a viewer who may not manage users at all", async () => {
    serve([rahul]);
    renderAs({ ...adminViewer, capabilities: ["crm.access_own"] });
    await openActions("Rahul Sharma");
    expect(offers("Reset password")).toBe(false);
    expect(offers("Delete account")).toBe(false);
  });
});

describe("the details panel", () => {
  it("offers Reset password beside Set temporary password and Access as user for an active User", async () => {
    serve([rahul]);
    renderAs(adminViewer);
    const { drawer } = await openDetails("Rahul Sharma");
    const buttons = within(drawer).getAllByRole("button").map((b) => b.textContent);
    expect(buttons).toEqual(expect.arrayContaining(["Reset password", "Set temporary password", "Access as user"]));
    // The reset link comes first: it is the way to help someone who forgot their password.
    expect(buttons.indexOf("Reset password")).toBeLessThan(buttons.indexOf("Set temporary password"));
    expect(within(drawer).queryByText(/Not available/)).not.toBeInTheDocument();
  });

  it("offers an Admin's owner a reset link only, and says what is not available for them", async () => {
    serve([admin]);
    renderAs(adminViewer);
    const { drawer } = await openDetails("Tara Test");
    expect(within(drawer).getByRole("button", { name: "Reset password" })).toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Set temporary password" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Access as user" })).not.toBeInTheDocument();
    expect(drawer).toHaveTextContent("Temporary password and support access: Not available for administrators.");
    expect(drawer).not.toHaveTextContent("Reset password: Not available");
  });

  it.each([
    ["invited", invitedAdmin, "Not available until they accept their invitation."],
    ["deactivated", deactivatedAdmin, "Not available for deactivated users."],
  ] as const)("explains why an %s Admin gets no reset link", async (_status, target, reason) => {
    serve([target]);
    renderAs(adminViewer);
    const { drawer } = await openDetails(target.full_name);
    expect(within(drawer).queryByRole("button", { name: "Reset password" })).not.toBeInTheDocument();
    expect(drawer).toHaveTextContent("Temporary password and support access: Not available for administrators.");
    expect(drawer).toHaveTextContent(`Reset password: ${reason}`);
  });

  it.each([
    ["an invited User", invitedUser, "Not available until they accept their invitation."],
    ["a deactivated User", deactivatedUser, "Not available for deactivated users."],
    ["a Super admin", superAdmin, "Not available for administrators."],
    ["the Super admin themselves", superSelf, "This is you. Change your own password in Settings."],
    ["a deleted account", former, "This former user was pseudonymised: their account can't be changed."],
  ] as const)("says why %s has no password tools at all", async (_who, target, reason) => {
    serve([target]);
    renderAs(adminViewer);
    const { drawer } = await openDetails(target.full_name);
    expect(drawer).toHaveTextContent(reason);
    expect(within(drawer).queryByRole("button", { name: "Reset password" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Set temporary password" })).not.toBeInTheDocument();
    expect(drawer).not.toHaveTextContent("Temporary password and support access:");
  });

  it("an Admin's team member has the same tools for an Admin viewer", async () => {
    serve([adminSelf, rahul]);
    renderAs(teamAdminViewer);
    const { drawer } = await openDetails("Rahul Sharma");
    expect(within(drawer).getByRole("button", { name: "Reset password" })).toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: "Set temporary password" })).toBeInTheDocument();
  });
});

describe("sending the link", () => {
  it("confirms first: whose account, what happens, and nothing is sent until then", async () => {
    const api = serve([rahul]);
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));

    const dialog = screen.getByRole("alertdialog", { name: "Reset password for Rahul Sharma?" });
    // Whose account: name, email and role.
    expect(within(dialog).getByText("Rahul Sharma")).toBeInTheDocument();
    expect(within(dialog).getByText("rahul@example.test")).toBeInTheDocument();
    expect(within(dialog).getByText("User")).toBeInTheDocument();
    // No field at all: the administrator never types or chooses a password.
    expect(dialog.querySelector("input, textarea, select")).toBeNull();
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("single-use link"));
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("current password stops working right away"));
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("signed out everywhere"));
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("You never see or choose their password"));
    // Consequential (they are signed out at once): focus starts on the safe choice.
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "reset-password"))).toHaveLength(0);
  });

  it("names the Admin's role when the account is an Admin's", async () => {
    serve([admin]);
    renderAs(adminViewer);
    const user = await openActions("Tara Test");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    const dialog = screen.getByRole("alertdialog", { name: "Reset password for Tara Test?" });
    expect(within(dialog).getByText("Admin")).toBeInTheDocument();
  });

  it("sends the link from the row's menu and says where it went", async () => {
    const api = serve([rahul], { [RESET(rahul)]: { status: 200, body: { ...rahul, version: 2 } } });
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Send reset link" }));

    const notice = await screen.findByText("Reset link sent to rahul@example.test.");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    // The address in it wraps on a phone instead of running off the screen.
    expect(notice.closest('[aria-live="polite"]')).toHaveClass("[overflow-wrap:anywhere]");
    // No body at all: the administrator chooses nothing.
    expect(api.callsTo("POST", path(rahul, "reset-password"))).toHaveLength(1);
    expect(api.callsTo("POST", path(rahul, "reset-password"))[0]!.body).toBeUndefined();
    // The list and the security events are read again.
    await waitFor(() => expect(listCalls(api).length).toBeGreaterThan(1));
  });

  it("sends the link from the details panel and returns there with the outcome", async () => {
    const api = serve([rahul], { [RESET(rahul)]: { status: 200, body: { ...rahul, version: 2 } } });
    renderAs(adminViewer);
    const { user, drawer } = await openDetails("Rahul Sharma");
    await user.click(within(drawer).getByRole("button", { name: "Reset password" }));
    expect(screen.queryByRole("dialog", { name: "Rahul Sharma" })).not.toBeInTheDocument(); // no stacked modals
    await user.click(within(screen.getByRole("alertdialog", { name: "Reset password for Rahul Sharma?" })).getByRole("button", { name: "Send reset link" }));

    const back = await screen.findByRole("dialog", { name: "Rahul Sharma" });
    const notice = await within(back).findByRole("status");
    expect(notice).toHaveTextContent("Reset link sent to rahul@example.test.");
    expect(notice.closest('[aria-live="polite"]')).toHaveClass("[overflow-wrap:anywhere]");
    expect(api.callsTo("POST", path(rahul, "reset-password"))).toHaveLength(1);
  });

  it("shows the button busy while the link is sent, and sends it only once", async () => {
    let release!: (reply: { status: number; body: unknown }) => void;
    const reply = new Promise<{ status: number; body: unknown }>((resolve) => (release = resolve));
    const api = serve([rahul], { [RESET(rahul)]: () => reply });
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    const dialog = screen.getByRole("alertdialog");
    const send = within(dialog).getByRole("button", { name: "Send reset link" });
    await user.click(send);
    await waitFor(() => expect(send).toHaveAttribute("aria-busy", "true"));
    await user.click(send);
    // Nothing can dismiss it half-way: not Escape, not Cancel.
    await user.keyboard("{Escape}");
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(api.callsTo("POST", path(rahul, "reset-password"))).toHaveLength(1);

    release({ status: 200, body: { ...rahul, version: 2 } });
    expect(await screen.findByText("Reset link sent to rahul@example.test.")).toBeInTheDocument();
    expect(api.callsTo("POST", path(rahul, "reset-password"))).toHaveLength(1);
  });

  it("asks for a recent second step when the API wants one, then sends the link once", async () => {
    let verified = false;
    const handler = vi.fn(async () => {
      verified = true;
      return true;
    });
    const unregister = registerStepUpHandler(handler);
    try {
      const api = serve([rahul], {
        [RESET(rahul)]: () =>
          verified ? { status: 200, body: { ...rahul, version: 2 } } : apiError(403, "mfa_required", "Confirm it's you first."),
      });
      renderAs(adminViewer);
      const user = await openActions("Rahul Sharma");
      await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
      await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Send reset link" }));
      expect(await screen.findByText("Reset link sent to rahul@example.test.")).toBeInTheDocument();
      expect(handler).toHaveBeenCalledTimes(1);
      expect(api.callsTo("POST", path(rahul, "reset-password"))).toHaveLength(2);
    } finally {
      unregister();
    }
  });

  it("says so when the second step is cancelled, and nothing was sent", async () => {
    const unregister = registerStepUpHandler(async () => false);
    try {
      const api = serve([rahul], { [RESET(rahul)]: apiError(403, "mfa_required", "Confirm it's you first.") });
      renderAs(adminViewer);
      const user = await openActions("Rahul Sharma");
      await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
      const dialog = screen.getByRole("alertdialog");
      await user.click(within(dialog).getByRole("button", { name: "Send reset link" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "Confirm it's you with a code from your authenticator app to do that.",
      );
      expect(api.callsTo("POST", path(rahul, "reset-password"))).toHaveLength(1);
    } finally {
      unregister();
    }
  });
});

describe("when the link can't be sent", () => {
  const LIMIT =
    "A reset link was already sent to this account several times in the last hour. Ask them to use the latest one, or try again later.";

  it.each([
    [
      "a Super admin's account (403)",
      apiError(403, "permission_denied", "A super admin's account is managed only by that super admin: it can't be reset or deleted here."),
      "A super admin's account is managed only by that super admin: it can't be reset or deleted here.",
    ],
    [
      "an account that isn't active (422)",
      apiError(422, "business_rule_violation", "Only active accounts can be sent a reset link. Resend the invitation to a user who hasn't accepted it; a deactivated account has to be reactivated first."),
      "Only active accounts can be sent a reset link.",
    ],
    [
      "the account's own limit of 3 links an hour (429)",
      { ...apiError(429, "rate_limited", LIMIT), headers: { "Retry-After": "3600" } },
      "A reset link was already sent to this account several times in the last hour. Ask them to use the latest one, or try again in about an hour.",
    ],
    [
      "the same limit without a Retry-After (429)",
      apiError(429, "rate_limited", LIMIT),
      "Ask them to use the latest one, or try again later.",
    ],
    [
      "the general request throttle (429)",
      { ...apiError(429, "rate_limited", "Request was throttled. Expected available in 37 seconds."), headers: { "Retry-After": "37" } },
      "Too many reset links. Try again in about a minute.",
    ],
    ["a server error (500)", apiError(500, "server_error", "Boom"), "Something went wrong on our side. Please try again in a moment."],
  ])("explains %s", async (_what, reply, shown) => {
    serve([rahul], { [RESET(rahul)]: reply });
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Send reset link" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(shown);
    // The words of the app, never the throttle's or a raw server error.
    expect(alert).not.toHaveTextContent("Request was throttled");
    expect(alert).not.toHaveTextContent("3600 seconds");
    expect(alert).not.toHaveTextContent("Boom");
    // The dialog stays, with the button ready again (and Cancel too).
    expect(within(dialog).getByRole("button", { name: "Send reset link" })).not.toHaveAttribute("aria-busy");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("quotes the reference of a server error", async () => {
    serve([rahul], { [RESET(rahul)]: apiError(500, "server_error", "Boom") });
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Send reset link" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Reference: req-test-1");
  });

  it("reads the list again after a refusal the page couldn't foresee (422), so the row's menu is current", async () => {
    const api = serve([rahul], { [RESET(rahul)]: apiError(422, "business_rule_violation", "Only active accounts can be sent a reset link.") });
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Send reset link" }));
    await screen.findByText("Only active accounts can be sent a reset link.");
    await waitFor(() => expect(listCalls(api).length).toBeGreaterThan(1));
  });

  it("closes and says so when the account is out of reach now (404)", async () => {
    const api = serve([rahul], { [RESET(rahul)]: apiError(404, "not_found", "Not found.") });
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Rahul Sharma is no longer in your reach (they may have moved to another team). Nothing was changed, and the list has been refreshed.",
    );
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(listCalls(api).length).toBeGreaterThan(1));
  });
});

describe("accessibility", () => {
  it("every control has a name, and the fields and dialog are labelled", async () => {
    serve([rahul]);
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    const dialog = screen.getByRole("alertdialog", { name: "Reset password for Rahul Sharma?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(unnamedControls(document.body)).toEqual([]);
    expect(within(dialog).getAllByRole("button").map((b) => b.getAttribute("aria-label") ?? b.textContent)).toEqual([
      "Close dialog",
      "Cancel",
      "Send reset link",
    ]);
  });

  it("Escape closes it, and focus returns to the row's menu button", async () => {
    serve([rahul]);
    renderAs(adminViewer);
    const user = await openActions("Rahul Sharma");
    await user.click(screen.getByRole("menuitem", { name: "Reset password" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actions for Rahul Sharma" })).toHaveFocus();
  });
});
