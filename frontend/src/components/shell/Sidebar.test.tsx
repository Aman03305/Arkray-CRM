import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Viewer } from "@/lib/viewer";
import { makeViewer } from "@/test/fixtures";
import { renderWithProviders } from "@/test/render";

import { Sidebar } from "./Sidebar";

const navigation = vi.hoisted(() => ({ pathname: "/dashboard" }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname }));

const RAHUL = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";
const salesUser: Viewer = makeViewer({ id: "u1", firstName: "Priya", lastName: "Patel", email: "priya@example.test", capabilities: ["crm.access_own", "ai.query"] });
const admin: Viewer = makeViewer({ id: "a1", firstName: "Anita", lastName: "Admin", email: "admin@example.test", capabilities: ["crm.access_own", "crm.view_all", "workspace.view_any", "users.manage"] });

function renderSidebar(viewer: Viewer | null) {
  return renderWithProviders(<Sidebar />, { viewer });
}

describe("Sidebar", () => {
  beforeEach(() => {
    navigation.pathname = "/dashboard";
  });

  it("shows the product name, four modules, Settings and the current user", () => {
    renderSidebar(salesUser);
    expect(screen.getByText("Arkray CRM")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link").map((l) => l.textContent)).toEqual([
      "Dashboard",
      "Pipeline",
      "Leads",
      "Activities",
    ]);
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    expect(screen.getByText("Priya Patel")).toBeInTheDocument();
  });

  it("hides administration from sales users", () => {
    renderSidebar(salesUser);
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    expect(screen.queryByText(/companies|products/i)).not.toBeInTheDocument();
  });

  it("shows Users to admins", () => {
    renderSidebar(admin);
    expect(screen.getByRole("link", { name: "Users" })).toHaveAttribute("href", "/admin/users");
  });

  it("marks the active module", () => {
    navigation.pathname = "/leads";
    renderSidebar(salesUser);
    expect(screen.getByRole("link", { name: "Leads" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Dashboard" })).not.toHaveAttribute("aria-current");
  });

  it("leads a sales user on an administrator's link back to their own workspace", () => {
    // Whole-software audit: every link pointed into the user's workspace, all "not found".
    navigation.pathname = `/admin/users/${RAHUL}/leads`;
    renderSidebar(salesUser);
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Leads" })).toHaveAttribute("href", "/leads");
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toHaveAttribute("href", "/dashboard");
    expect(within(nav).queryByText(/selected user/i)).not.toBeInTheDocument();
  });

  it("keeps an admin inside the selected user's workspace while navigating", () => {
    navigation.pathname = `/admin/users/${RAHUL}/dashboard`;
    renderSidebar(admin);
    expect(screen.getByRole("link", { name: "Pipeline" })).toHaveAttribute("href", `/admin/users/${RAHUL}/pipeline`);
    expect(screen.getByRole("link", { name: "Leads" })).toHaveAttribute("href", `/admin/users/${RAHUL}/leads`);
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
  });

  it("renders a loading state instead of guessing while the viewer loads", () => {
    renderSidebar(null);
    expect(screen.getByText("Loading user")).toBeInTheDocument();
  });
});

describe("Sidebar sign-out", () => {
  it("offers sign out next to the current user", () => {
    renderSidebar(salesUser);
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });
});
