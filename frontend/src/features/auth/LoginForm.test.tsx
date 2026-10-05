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
});
