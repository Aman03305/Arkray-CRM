import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { adminViewer } from "@/test/fixtures";
import { darkModules, makeMfaStatus, MFA_SETUP, QR_FIXTURE_DARK_MODULES, RECOVERY_CODES } from "@/test/mfa-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { ProfilePage } from "./ProfilePage";

const nav = vi.hoisted(() => ({ hardNavigate: vi.fn() }));
vi.mock("@/lib/browser", () => ({ hardNavigate: nav.hardNavigate, currentLocation: () => "/settings" }));

beforeEach(() => nav.hardNavigate.mockReset());

/** Settings loads the two-step status too. */
const MFA_OFF = { "GET /api/v1/auth/mfa": { status: 200, body: makeMfaStatus() } };

function passwordSection() {
  return screen.getByRole("region", { name: "Password" });
}

async function changePassword(current: string, next: string, confirmation = next) {
  const user = userEvent.setup();
  const section = passwordSection();
  await user.type(within(section).getByLabelText("Current password"), current);
  await user.type(within(section).getByLabelText("New password"), next);
  await user.type(within(section).getByLabelText("Confirm password"), confirmation);
  await user.click(within(section).getByRole("button", { name: "Change password" }));
}

describe("ProfilePage", () => {
  it("shows name, email and role", () => {
    mockApi({ ...MFA_OFF });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    const profile = screen.getByRole("region", { name: "Profile" });
    expect(profile).toHaveTextContent("Anita Admin");
    expect(profile).toHaveTextContent("admin@example.test");
    expect(profile).toHaveTextContent("Admin");
  });

  it("changes the password and says other devices were signed out", async () => {
    const api = mockApi({ ...MFA_OFF, "POST /api/v1/auth/password/change": { status: 204 } });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    await changePassword("old passphrase", "a new long passphrase");
    expect(await within(passwordSection()).findByRole("status")).toHaveTextContent("signed out on your other devices");
    expect(api.callsTo("POST", "/api/v1/auth/password/change")[0]!.body).toEqual({
      current_password: "old passphrase",
      new_password: "a new long passphrase",
    });
    expect(within(passwordSection()).getByLabelText("Current password")).toHaveValue("");
  });

  it("puts a wrong current password on its field", async () => {
    mockApi({
      ...MFA_OFF,
      "POST /api/v1/auth/password/change": apiError(400, "validation_error", "Your current password is incorrect.", {
        current_password: ["Your current password is incorrect."],
      }),
    });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    await changePassword("wrong", "a new long passphrase");
    await waitFor(() =>
      expect(within(passwordSection()).getByLabelText("Current password")).toHaveAccessibleDescription(
        "Your current password is incorrect.",
      ),
    );
  });

  it("signs out and reloads onto the sign-in page", async () => {
    const api = mockApi({ ...MFA_OFF, "POST /api/v1/auth/logout": { status: 204 } });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/login?reason=signed-out", "signed-out"));
    expect(api.callsTo("POST", "/api/v1/auth/logout")).toHaveLength(1);
  });

  it("stays put and explains if signing out fails", async () => {
    mockApi({ ...MFA_OFF, "POST /api/v1/auth/logout": apiError(503, "service_unavailable", "Down.") });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Arkray is temporarily unavailable");
    expect(nav.hardNavigate).not.toHaveBeenCalled();
  });
});

describe("ProfilePage: two-step verification (ADR-0033)", () => {
  function twoStep() {
    return screen.getByRole("region", { name: "Two-step verification" });
  }

  it("shows it's off and sets it up: password, QR code, a first code, then the recovery codes once", async () => {
    let enabled = false;
    const api = mockApi({
      "GET /api/v1/auth/mfa": () => ({
        status: 200,
        body: enabled ? makeMfaStatus({ enabled: true, confirmed_at: "2026-10-11T09:00:00Z", recovery_codes_remaining: 10 }) : makeMfaStatus(),
      }),
      "POST /api/v1/auth/mfa/setup": { status: 200, body: MFA_SETUP },
      "POST /api/v1/auth/mfa/confirm": () => {
        enabled = true;
        return { status: 200, body: { recovery_codes: RECOVERY_CODES } };
      },
    });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    const section = twoStep();
    expect(await within(section).findByText("Off")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(within(section).getByRole("button", { name: "Set up two-step verification" }));
    await user.type(within(section).getByLabelText("Current password"), "my passphrase");
    await user.click(within(section).getByRole("button", { name: "Continue" }));
    const qr = await within(section).findByRole("img", { name: /QR code/ });
    expect(darkModules(qr)).toBe(QR_FIXTURE_DARK_MODULES);
    await user.type(within(section).getByLabelText("Authentication code"), "123456");
    await user.click(within(section).getByRole("button", { name: "Turn on two-step verification" }));
    await within(section).findByRole("list", { name: "Recovery codes" });
    await user.click(within(section).getByRole("checkbox", { name: "I have saved these codes" }));
    await user.click(within(section).getByRole("button", { name: "Done" }));

    expect(await within(section).findByText("On")).toBeInTheDocument();
    expect(within(section).getByRole("status")).toHaveTextContent("Two-step verification is on");
    expect(within(section).getByText("10 left")).toBeInTheDocument();
    // Shown once: the codes and the key are gone.
    expect(within(section).queryByRole("list", { name: "Recovery codes" })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(RECOVERY_CODES[0]);
    expect(api.callsTo("POST", "/api/v1/auth/mfa/setup")).toHaveLength(1);
  });

  it("can cancel the set-up", async () => {
    mockApi({ ...MFA_OFF });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(await within(twoStep()).findByRole("button", { name: "Set up two-step verification" }));
    await user.click(within(twoStep()).getByRole("button", { name: "Cancel" }));
    expect(within(twoStep()).getByRole("button", { name: "Set up two-step verification" })).toBeInTheDocument();
  });

  it("replaces the recovery codes with a code from the app, showing the new ones once", async () => {
    const api = mockApi({
      "GET /api/v1/auth/mfa": { status: 200, body: makeMfaStatus({ enabled: true, confirmed_at: "2026-10-01T09:00:00Z", recovery_codes_remaining: 2 }) },
      "POST /api/v1/auth/mfa/recovery-codes": { status: 200, body: { recovery_codes: RECOVERY_CODES } },
    });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    expect(await within(twoStep()).findByText(/2 left\. Replace them soon\./)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(within(twoStep()).getByRole("button", { name: "Replace recovery codes" }));
    const dialog = screen.getByRole("dialog", { name: "Replace recovery codes" });
    expect(within(dialog).getByLabelText("Authentication code")).toHaveFocus();

    // A code is needed first.
    await user.click(within(dialog).getByRole("button", { name: "Replace codes" }));
    expect(within(dialog).getByLabelText("Authentication code")).toHaveAccessibleDescription(/Enter the 6-digit code/);
    expect(api.callsTo("POST", "/api/v1/auth/mfa/recovery-codes")).toHaveLength(0);

    await user.type(within(dialog).getByLabelText("Authentication code"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Replace codes" }));
    const list = await within(dialog).findByRole("list", { name: "Recovery codes" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(10);
    expect(api.callsTo("POST", "/api/v1/auth/mfa/recovery-codes")[0]!.body).toEqual({ code: "123456" });
    await user.click(within(dialog).getByRole("checkbox", { name: "I have saved these codes" }));
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(RECOVERY_CODES[0]);
  });

  it("turns it off with the password and a code, after a warning", async () => {
    let enabled = true;
    const api = mockApi({
      "GET /api/v1/auth/mfa": () => ({ status: 200, body: makeMfaStatus({ enabled, recovery_codes_remaining: 8, can_disable: true }) }),
      "POST /api/v1/auth/mfa/disable": () => {
        enabled = false;
        return { status: 204 };
      },
    });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(await within(twoStep()).findByRole("button", { name: "Turn off" }));
    const dialog = screen.getByRole("alertdialog", { name: "Turn off two-step verification?" });
    expect(within(dialog).getByLabelText("Current password")).toHaveFocus();
    await user.type(within(dialog).getByLabelText("Current password"), "my passphrase");
    await user.type(within(dialog).getByLabelText("Authentication code"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.callsTo("POST", "/api/v1/auth/mfa/disable")[0]!.body).toEqual({ current_password: "my passphrase", code: "123456" });
    expect(await within(twoStep()).findByText("Off")).toBeInTheDocument();
  });

  it("doesn't offer to turn it off when the role requires it", async () => {
    mockApi({
      "GET /api/v1/auth/mfa": { status: 200, body: makeMfaStatus({ enabled: true, recovery_codes_remaining: 10, required: true, can_disable: false }) },
    });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    expect(await within(twoStep()).findByText("Required for your role")).toBeInTheDocument();
    expect(within(twoStep()).getByRole("button", { name: "Replace recovery codes" })).toBeInTheDocument();
    expect(within(twoStep()).queryByRole("button", { name: "Turn off" })).not.toBeInTheDocument();
  });

  it("isn't offered during a support session (the API refuses it there)", () => {
    const api = mockApi({ ...MFA_OFF });
    const supporting = {
      ...adminViewer,
      supportSession: {
        id: "s1",
        target: { id: "u7", fullName: "Rahul Sharma" },
        reason: "Fixing a lead",
        startedAt: "2026-10-11T09:00:00Z",
        expiresAt: "2026-10-11T09:30:00Z",
      },
    };
    renderWithProviders(<ProfilePage />, { viewer: supporting });
    expect(screen.queryByRole("region", { name: "Two-step verification" })).not.toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/auth/mfa")).toHaveLength(0);
  });
});
