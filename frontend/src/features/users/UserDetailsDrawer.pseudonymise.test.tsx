/**
 * Pseudonymising from a user's details is never offered for an administrator's account,
 * active or deactivated (ADR-0030, audit SEC-2): the API refuses it (403), and an operator
 * does it on the server if it must be done. Former sales users are offered it as before
 * (features/privacy/privacy.test.tsx).
 */
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { adminViewer, makeAdminUser } from "@/test/fixtures";
import { mockApi, renderWithProviders } from "@/test/render";

import { UsersPage } from "./UsersPage";

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/users",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

describe("pseudonymising an administrator's account", () => {
  it.each([
    ["deactivated", "Deactivated"],
    ["active", "Active"],
  ] as const)("is not offered for a %s administrator", async (status, statusLabel) => {
    const bina = makeAdminUser({
      first_name: "Bina",
      last_name: "Admin",
      full_name: "Bina Admin",
      email: "bina@example.test",
      role: "admin",
      role_label: "Administrator",
      status,
      status_label: statusLabel,
      deactivated_at: status === "deactivated" ? "2026-10-01T04:30:00Z" : null,
    });
    const api = mockApi({
      "GET /api/v1/admin/users": { status: 200, body: { results: [bina], next: null, previous: null } },
      [`GET /api/v1/admin/users/${bina.id}`]: { status: 200, body: bina },
    });
    renderWithProviders(<UsersPage />, { viewer: adminViewer });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Bina Admin" }));
    const drawer = screen.getByRole("dialog", { name: "Bina Admin" });
    expect(within(drawer).getByRole("button", { name: "Export data" })).toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Pseudonymise…" })).not.toBeInTheDocument();
    expect(drawer).toHaveTextContent("An administrator's account can't be pseudonymised here.");
    expect(api.callsTo("POST", `/api/v1/admin/privacy/users/${bina.id}/pseudonymise`)).toHaveLength(0);
  });
});
