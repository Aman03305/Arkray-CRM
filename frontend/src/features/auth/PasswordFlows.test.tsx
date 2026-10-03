import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { ActivateAccountForm } from "./ActivateAccountForm";
import { ForgotPasswordForm } from "./ForgotPasswordForm";
import { ResetPasswordForm } from "./ResetPasswordForm";

const nav = vi.hoisted(() => ({ hardNavigate: vi.fn() }));
vi.mock("@/lib/browser", () => ({ hardNavigate: nav.hardNavigate, currentLocation: () => "/" }));

const TOKEN = "A".repeat(43);
const GENERIC = "If an eligible account exists, password reset instructions have been sent.";
const INVALID = apiError(400, "invalid_token", "This link is invalid or has expired. Ask for a new one.");

async function choosePassword(label: string, password: string, confirmation = password) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(label), password);
  await user.type(screen.getByLabelText("Confirm password"), confirmation);
  return user;
}

beforeEach(() => nav.hardNavigate.mockReset());

describe("ForgotPasswordForm", () => {
  it("shows the server's account-neutral confirmation", async () => {
    const api = mockApi({ "POST /api/v1/auth/password-reset": { status: 202, body: { detail: GENERIC } } });
    renderWithProviders(<ForgotPasswordForm />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Email"), "someone@example.test");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("status")).toHaveTextContent(GENERIC);
    expect(api.callsTo("POST", "/api/v1/auth/password-reset")[0]!.body).toEqual({ email: "someone@example.test" });
  });

  it("reports rate limiting", async () => {
    mockApi({ "POST /api/v1/auth/password-reset": apiError(429, "rate_limited", "Too many password reset requests. Try again later.") });
    renderWithProviders(<ForgotPasswordForm />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Email"), "someone@example.test");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many password reset requests");
  });

  it("requires an email", async () => {
    const api = mockApi({});
    renderWithProviders(<ForgotPasswordForm />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Send reset link" }));
    expect(screen.getByLabelText("Email")).toHaveAccessibleDescription("Enter your email address.");
    expect(api.calls).toHaveLength(0);
  });
});

describe("ResetPasswordForm", () => {
  it("sets the new password, then sends the user to sign in", async () => {
    const api = mockApi({ "POST /api/v1/auth/password-reset/confirm": { status: 204 } });
    renderWithProviders(<ResetPasswordForm token={TOKEN} />);
    const user = await choosePassword("New password", "a fresh long passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/login?reason=reset"));
    expect(api.callsTo("POST", "/api/v1/auth/password-reset/confirm")[0]!.body).toEqual({
      token: TOKEN,
      new_password: "a fresh long passphrase",
    });
  });

  it("checks the confirmation locally", async () => {
    const api = mockApi({});
    renderWithProviders(<ResetPasswordForm token={TOKEN} />);
    const user = await choosePassword("New password", "a fresh long passphrase", "something else");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(screen.getByLabelText("Confirm password")).toHaveAccessibleDescription("The passwords don't match.");
    expect(api.calls).toHaveLength(0);
  });

  it("shows the password policy's reasons", async () => {
    mockApi({
      "POST /api/v1/auth/password-reset/confirm": apiError(400, "validation_error", "Choose a stronger password.", {
        new_password: ["This password is too common."],
      }),
    });
    renderWithProviders(<ResetPasswordForm token={TOKEN} />);
    const user = await choosePassword("New password", "password1234");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() =>
      expect(screen.getByLabelText("New password")).toHaveAccessibleDescription(
        expect.stringContaining("This password is too common."),
      ),
    );
  });

  it("offers a new link when this one is invalid, used or expired", async () => {
    mockApi({ "POST /api/v1/auth/password-reset/confirm": INVALID });
    renderWithProviders(<ResetPasswordForm token={TOKEN} />);
    const user = await choosePassword("New password", "a fresh long passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("heading", { name: "This link can't be used" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Request a new link" })).toHaveAttribute("href", "/forgot-password");
  });
});

describe("ActivateAccountForm", () => {
  const PREVIEW = { email: "neha@example.test", first_name: "Neha" };

  it("greets the invitee, then activates and sends them to sign in", async () => {
    const api = mockApi({
      "POST /api/v1/auth/invitations/verify": { status: 200, body: PREVIEW },
      "POST /api/v1/auth/invitations/accept": { status: 200, body: PREVIEW },
    });
    renderWithProviders(<ActivateAccountForm token={TOKEN} />);
    expect(screen.getByRole("status")).toHaveTextContent("One moment");
    expect(await screen.findByRole("heading", { name: "Welcome, Neha" })).toBeInTheDocument();
    expect(screen.getByText("neha@example.test")).toBeInTheDocument();

    const user = await choosePassword("Password", "a long invitation passphrase");
    await user.click(screen.getByRole("button", { name: "Activate account" }));
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/login?reason=activated"));
    expect(api.callsTo("POST", "/api/v1/auth/invitations/accept")[0]!.body).toEqual({
      token: TOKEN,
      password: "a long invitation passphrase",
    });
  });

  it("explains an unusable invitation without showing a form", async () => {
    mockApi({ "POST /api/v1/auth/invitations/verify": INVALID });
    renderWithProviders(<ActivateAccountForm token={TOKEN} />);
    expect(await screen.findByRole("heading", { name: "This invitation can't be used" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });

  it("switches to the explanation if the link is superseded meanwhile", async () => {
    mockApi({
      "POST /api/v1/auth/invitations/verify": { status: 200, body: PREVIEW },
      "POST /api/v1/auth/invitations/accept": INVALID,
    });
    renderWithProviders(<ActivateAccountForm token={TOKEN} />);
    await screen.findByRole("heading", { name: "Welcome, Neha" });
    const user = await choosePassword("Password", "a long invitation passphrase");
    await user.click(screen.getByRole("button", { name: "Activate account" }));
    expect(await screen.findByRole("heading", { name: "This invitation can't be used" })).toBeInTheDocument();
  });

  it("offers show/hide for the password", async () => {
    mockApi({ "POST /api/v1/auth/invitations/verify": { status: 200, body: PREVIEW } });
    renderWithProviders(<ActivateAccountForm token={TOKEN} />);
    const field = await screen.findByLabelText("Password");
    expect(field).toHaveAttribute("type", "password");
    await userEvent.setup().click(screen.getByRole("button", { name: "Show password" }));
    expect(field).toHaveAttribute("type", "text");
  });
});
