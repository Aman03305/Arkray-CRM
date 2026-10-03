import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { adminViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { ProfilePage } from "./ProfilePage";

const nav = vi.hoisted(() => ({ hardNavigate: vi.fn() }));
vi.mock("@/lib/browser", () => ({ hardNavigate: nav.hardNavigate, currentLocation: () => "/settings" }));

beforeEach(() => nav.hardNavigate.mockReset());

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
    mockApi({});
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    const profile = screen.getByRole("region", { name: "Profile" });
    expect(profile).toHaveTextContent("Anita Admin");
    expect(profile).toHaveTextContent("admin@example.test");
    expect(profile).toHaveTextContent("Admin");
  });

  it("changes the password and says other devices were signed out", async () => {
    const api = mockApi({ "POST /api/v1/auth/password/change": { status: 204 } });
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
    const api = mockApi({ "POST /api/v1/auth/logout": { status: 204 } });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/login?reason=signed-out", "signed-out"));
    expect(api.callsTo("POST", "/api/v1/auth/logout")).toHaveLength(1);
  });

  it("stays put and explains if signing out fails", async () => {
    mockApi({ "POST /api/v1/auth/logout": apiError(503, "service_unavailable", "Down.") });
    renderWithProviders(<ProfilePage />, { viewer: adminViewer });
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on our side");
    expect(nav.hardNavigate).not.toHaveBeenCalled();
  });
});
