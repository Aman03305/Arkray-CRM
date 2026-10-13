import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { LoginForm } from "./LoginForm";

const nav = vi.hoisted(() => ({ search: "", hardNavigate: vi.fn() }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(nav.search) }));
vi.mock("@/lib/browser", () => ({ hardNavigate: nav.hardNavigate, currentLocation: () => "/login" }));

const VIEWER = {
  id: "u1",
  email: "rahul@example.test",
  first_name: "Rahul",
  last_name: "Sharma",
  full_name: "Rahul Sharma",
  role: "sales_user",
  role_label: "User",
  capabilities: ["crm.access_own"],
};
const NOT_SIGNED_IN = apiError(401, "not_authenticated", "Authentication credentials were not provided.");

async function fillAndSubmit(email = "rahul@example.test", password = "my passphrase") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return user;
}

describe("LoginForm", () => {
  beforeEach(() => {
    nav.search = "";
    nav.hardNavigate.mockReset();
  });

  it("welcomes with the Arkray logo above the sign-in form", () => {
    mockApi({ "GET /api/v1/auth/me": NOT_SIGNED_IN });
    renderWithProviders(<LoginForm />);
    const logo = screen.getByRole("img", { name: "Arkray" });
    const heading = screen.getByRole("heading", { level: 1, name: "Welcome to Arkray" });
    expect(logo.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("Sign in with your work email and password.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText("Arkray CRM")).not.toBeInTheDocument();
  });

  it("signs in and reloads into the dashboard", async () => {
    const api = mockApi({ "GET /api/v1/auth/me": NOT_SIGNED_IN, "POST /api/v1/auth/login": { status: 200, body: VIEWER } });
    renderWithProviders(<LoginForm />);
    await fillAndSubmit();
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/dashboard", "signed-in"));
    const [call] = api.callsTo("POST", "/api/v1/auth/login");
    expect(call!.body).toEqual({ email: "rahul@example.test", password: "my passphrase" });
    expect(call!.headers["X-CSRFToken"]).toBe("test-csrf-token");
  });

  it("returns to a safe `next` page, never to another site", async () => {
    mockApi({ "GET /api/v1/auth/me": NOT_SIGNED_IN, "POST /api/v1/auth/login": { status: 200, body: VIEWER } });
    nav.search = "next=%2Fleads%3Fstatus%3Dnew";
    const first = renderWithProviders(<LoginForm />);
    await fillAndSubmit();
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenLastCalledWith("/leads?status=new", "signed-in"));
    first.unmount();

    nav.search = "next=https%3A%2F%2Fevil.example";
    renderWithProviders(<LoginForm />);
    await fillAndSubmit();
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenLastCalledWith("/dashboard", "signed-in"));
  });

  it("shows the server's generic message for bad credentials", async () => {
    mockApi({
      "GET /api/v1/auth/me": NOT_SIGNED_IN,
      "POST /api/v1/auth/login": apiError(400, "invalid_credentials", "Invalid email or password."),
    });
    renderWithProviders(<LoginForm />);
    await fillAndSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password.");
    expect(nav.hardNavigate).not.toHaveBeenCalled();
  });

  it("explains an expired temporary password with the server's message", async () => {
    const message = "Your temporary password has expired. Ask your administrator for a new one.";
    mockApi({
      "GET /api/v1/auth/me": NOT_SIGNED_IN,
      "POST /api/v1/auth/login": apiError(400, "temporary_password_expired", message, { password: ["Expired."] }),
    });
    renderWithProviders(<LoginForm />);
    await fillAndSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("alert")).not.toHaveTextContent("Reference");
    expect(nav.hardNavigate).not.toHaveBeenCalled();
  });

  it("explains a lockout", async () => {
    mockApi({
      "GET /api/v1/auth/me": NOT_SIGNED_IN,
      "POST /api/v1/auth/login": apiError(429, "rate_limited", "Too many sign-in attempts. Try again in 1 minute."),
    });
    renderWithProviders(<LoginForm />);
    await fillAndSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many sign-in attempts. Try again in 1 minute.");
  });

  it("says how to get back in when too many failed sign-ins locked the account (review of 73ff1e3, L1)", async () => {
    const message =
      "Too many failed sign-ins for this account, so it can't be signed in to from a new browser for now. Sign in from a browser you've used here before, use the link we emailed to the account's address, or reset your password.";
    mockApi({
      "GET /api/v1/auth/me": NOT_SIGNED_IN,
      "POST /api/v1/auth/login": { ...apiError(429, "sign_in_locked", message), headers: { "Retry-After": "2591000" } },
    });
    renderWithProviders(<LoginForm />);
    await fillAndSubmit();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).not.toHaveTextContent("Try again");
  });

  it("validates required fields without calling the API", async () => {
    const api = mockApi({ "GET /api/v1/auth/me": NOT_SIGNED_IN });
    renderWithProviders(<LoginForm />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByLabelText("Email")).toHaveAccessibleDescription("Enter your email address.");
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
    expect(api.callsTo("POST", "/api/v1/auth/login")).toHaveLength(0);
  });

  it("cannot be submitted twice while signing in", async () => {
    let release: () => void = () => undefined;
    const api = mockApi({
      "GET /api/v1/auth/me": NOT_SIGNED_IN,
      "POST /api/v1/auth/login": () =>
        new Promise((resolve) => {
          release = () => resolve({ status: 200, body: VIEWER });
        }),
    });
    renderWithProviders(<LoginForm />);
    const user = await fillAndSubmit();
    const button = screen.getByRole("button", { name: "Sign in" });
    await waitFor(() => expect(button).toHaveAttribute("aria-disabled", "true"));
    expect(button).toHaveFocus(); // focus is kept while busy (aria-disabled, not disabled)
    await user.click(button);
    release();
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalled());
    expect(api.callsTo("POST", "/api/v1/auth/login")).toHaveLength(1);
  });

  it.each([
    ["expired", "Your session has ended. Please sign in again."],
    ["signed-out", "You have been signed out."],
    ["activated", "Your account is active. Sign in with your new password."],
    ["reset", "Your password has been changed. Sign in with your new password."],
  ])("explains why the user is here (%s)", async (reason, text) => {
    mockApi({ "GET /api/v1/auth/me": NOT_SIGNED_IN });
    nav.search = `reason=${reason}`;
    renderWithProviders(<LoginForm />);
    expect(screen.getByRole("status")).toHaveTextContent(text);
  });

  it("sends an already signed-in user straight on", async () => {
    mockApi({ "GET /api/v1/auth/me": { status: 200, body: VIEWER } });
    renderWithProviders(<LoginForm />);
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/dashboard"));
  });

  it("links to password recovery and labels every field", () => {
    mockApi({ "GET /api/v1/auth/me": NOT_SIGNED_IN });
    renderWithProviders(<LoginForm />);
    expect(screen.getByRole("link", { name: "Forgot password?" })).toHaveAttribute("href", "/forgot-password");
    expect(screen.getByLabelText("Email")).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it.each([
    ["the request throttle", "Request was throttled. Expected available in 37 seconds.", "37", "Try again in about a minute."],
    ["the account lockout", "Too many sign-in attempts. Try again in 15 minutes.", "900", "Try again in about 15 minutes."],
  ])("says plainly when sign-in is throttled by %s, never the server's raw text (E2E-2)", async (_, raw, wait, when) => {
    mockApi({
      "GET /api/v1/auth/me": NOT_SIGNED_IN,
      "POST /api/v1/auth/login": { ...apiError(429, "rate_limited", raw), headers: { "Retry-After": wait } },
    });
    renderWithProviders(<LoginForm />);
    await fillAndSubmit();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Too many sign-in attempts. ${when}`);
    expect(alert).not.toHaveTextContent(/throttled|Expected available/);
  });

  it("doesn't leave the password in the mutation cache once the form is gone", async () => {
    mockApi({
      "GET /api/v1/auth/me": NOT_SIGNED_IN,
      "POST /api/v1/auth/login": apiError(400, "invalid_credentials", "Invalid email or password."),
    });
    const { client, unmount } = renderWithProviders(<LoginForm />);
    await fillAndSubmit("rahul@example.test", "a secret passphrase");
    await screen.findByRole("alert");
    const cached = () => JSON.stringify(client.getMutationCache().getAll().map((mutation) => mutation.state.variables));
    // While the form shows the refusal its mutation is still there (as a retry would need)...
    expect(cached()).toContain("a secret passphrase");
    unmount();
    // ...and once the form is gone, so is the mutation (gcTime 0; the default keeps it 5 minutes).
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0));
    expect(cached()).not.toContain("a secret passphrase");
  });
});

describe("LoginForm: two-step verification (ADR-0033)", () => {
  const CHALLENGE = { status: 202, body: { mfa_required: true, expires_in: 300 } };

  beforeEach(() => {
    nav.search = "";
    nav.hardNavigate.mockReset();
  });

  async function reachSecondStep(routes: Parameters<typeof mockApi>[0]) {
    const api = mockApi({ "GET /api/v1/auth/me": NOT_SIGNED_IN, "POST /api/v1/auth/login": CHALLENGE, ...routes });
    const rendered = renderWithProviders(<LoginForm />);
    const user = await fillAndSubmit();
    expect(await screen.findByRole("heading", { name: "Two-step verification" })).toBeInTheDocument();
    return { api, user, ...rendered };
  }

  it("asks for the code after the password, then signs in exactly like a password sign-in", async () => {
    const { api, user, client } = await reachSecondStep({ "POST /api/v1/auth/login/mfa": { status: 200, body: VIEWER } });
    expect(nav.hardNavigate).not.toHaveBeenCalled(); // no session yet
    const field = screen.getByLabelText("Authentication code");
    expect(field).toHaveFocus();
    expect(field).toHaveAttribute("autocomplete", "one-time-code");
    expect(field).toHaveAttribute("inputmode", "numeric");
    // The password is gone from the mutation cache once it has done its job.
    expect(JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables))).not.toContain("my passphrase");

    await user.type(field, "123 456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/dashboard", "signed-in"));
    const [call] = api.callsTo("POST", "/api/v1/auth/login/mfa");
    expect(call!.body).toEqual({ code: "123456" });
    expect(call!.headers["X-CSRFToken"]).toBe("test-csrf-token");
  });

  it("checks the code's shape before sending", async () => {
    const { api, user } = await reachSecondStep({});
    await user.type(screen.getByLabelText("Authentication code"), "12");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(screen.getByLabelText("Authentication code")).toHaveAccessibleDescription(/Enter the 6-digit code/);
    expect(api.callsTo("POST", "/api/v1/auth/login/mfa")).toHaveLength(0);
  });

  it("puts a wrong code on the field and stays on the second step", async () => {
    const { user } = await reachSecondStep({
      "POST /api/v1/auth/login/mfa": apiError(400, "invalid_mfa_code", "That code isn't right. Try again."),
    });
    await user.type(screen.getByLabelText("Authentication code"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    const field = screen.getByLabelText("Authentication code");
    await waitFor(() => expect(field).toHaveAccessibleDescription(/That code isn't right\. Try again\./));
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveFocus();
    expect(nav.hardNavigate).not.toHaveBeenCalled();
  });

  it("goes back to the password with the server's explanation when the challenge expired", async () => {
    const message = "Your sign-in took too long. Enter your password again.";
    const { user } = await reachSecondStep({ "POST /api/v1/auth/login/mfa": apiError(400, "mfa_challenge_expired", message) });
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("heading", { name: "Welcome to Arkray" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("Email")).toHaveValue("rahul@example.test");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByLabelText("Password")).toHaveFocus();
  });

  it("goes back to the password, pointing to Forgot password, when too many wrong codes locked it", async () => {
    const { user } = await reachSecondStep({
      "POST /api/v1/auth/login/mfa": apiError(403, "mfa_locked", "Locked."),
    });
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("heading", { name: "Welcome to Arkray" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/Too many wrong codes were entered, so two-step verification is locked/);
    expect(screen.getByRole("alert")).toHaveTextContent(/Reset your password/);
    expect(screen.getByRole("link", { name: /Forgot password/ })).toBeInTheDocument();
  });

  it("signs in with a recovery code instead", async () => {
    const { api, user } = await reachSecondStep({ "POST /api/v1/auth/login/mfa": { status: 200, body: VIEWER } });
    await user.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    const field = screen.getByLabelText("Recovery code");
    expect(field).toHaveFocus();
    expect(screen.queryByLabelText("Authentication code")).not.toBeInTheDocument();
    await user.type(field, " ABCD-EFGH-JKLM-NPQR ");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/dashboard", "signed-in"));
    expect(api.callsTo("POST", "/api/v1/auth/login/mfa")[0]!.body).toEqual({ recovery_code: "ABCD-EFGH-JKLM-NPQR" });
  });

  it("explains when too many codes were tried, as the password step does", async () => {
    const { user } = await reachSecondStep({
      "POST /api/v1/auth/login/mfa": { ...apiError(429, "rate_limited", "Request was throttled. Expected available in 60 seconds."), headers: { "Retry-After": "60" } },
    });
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Too many sign-in attempts. Try again in about a minute.");
    expect(alert).not.toHaveTextContent(/throttled/);
    expect(screen.getByLabelText("Authentication code")).not.toHaveAttribute("aria-invalid");
  });

  it("Back returns to the email and password", async () => {
    const { api, user } = await reachSecondStep({});
    await user.click(screen.getByRole("button", { name: "Back to sign in" }));
    expect(await screen.findByRole("heading", { name: "Welcome to Arkray" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/auth/login/mfa")).toHaveLength(0);
  });
});
