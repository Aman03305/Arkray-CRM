import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { UnlockSignIn } from "./UnlockSignIn";

const UNLOCK = "POST /api/v1/auth/sign-in-unlock";
const VERIFY = "POST /api/v1/auth/sign-in-unlock/verify";
const MASKED = { status: 200, body: { email: "r***@example.test" } };
const TOKEN = "a".repeat(43);

describe("UnlockSignIn (review of 73ff1e3, L1)", () => {
  it("says at once when the link can't be used (checked without using it)", async () => {
    const api = mockApi({ [VERIFY]: apiError(400, "invalid_token", "This link is invalid or has expired. Ask for a new one.") });
    renderWithProviders(<UnlockSignIn token={TOKEN} />);
    expect(await screen.findByText("This link is invalid or has expired. Ask for a new one.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unlock sign-in" })).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/auth/sign-in-unlock")).toHaveLength(0);
  });

  it("lifts the lock only when asked, with the link's secret in the body", async () => {
    const api = mockApi({ [VERIFY]: MASKED, [UNLOCK]: { status: 204 } });
    renderWithProviders(<UnlockSignIn token={TOKEN} />);
    expect(screen.getByRole("heading", { name: "Unlock sign-in to your account" })).toBeInTheDocument();
    // Review round 1: whose account it is, masked (the link might be someone else's).
    expect(await screen.findByText(/r\*\*\*@example\.test/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/auth/sign-in-unlock/verify")[0]!.body).toEqual({ token: TOKEN });
    expect(api.callsTo("POST", "/api/v1/auth/sign-in-unlock")).toHaveLength(0); // a mail scanner opening it changes nothing
    await userEvent.setup().click(screen.getByRole("button", { name: "Unlock sign-in" }));
    expect(await screen.findByText("You can sign in from this browser again.")).toBeInTheDocument();
    const [call] = api.callsTo("POST", "/api/v1/auth/sign-in-unlock");
    expect(call!.body).toEqual({ token: TOKEN });
    expect(screen.getByRole("link", { name: "Back to sign in" })).toHaveAttribute("href", "/login");
  });

  it("explains a used or expired link and points to a password reset", async () => {
    mockApi({ [VERIFY]: MASKED, [UNLOCK]: apiError(400, "invalid_token", "This link is invalid or has expired. Ask for a new one.") });
    renderWithProviders(<UnlockSignIn token={TOKEN} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Unlock sign-in" }));
    expect(await screen.findByText("This link is invalid or has expired. Ask for a new one.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reset your password" })).toHaveAttribute("href", "/forgot-password");
  });
});
