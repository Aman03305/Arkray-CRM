import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { adminViewer, makeAdminUser, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { makeBoard, PIPELINES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { UserWorkspaceFrame } from "./UserWorkspaceFrame";
import { ActivitiesView, DashboardView, PipelineView } from "./views";

const navigation = vi.hoisted(() => ({ pathname: "/dashboard" }));
vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

beforeEach(() => {
  navigation.pathname = "/dashboard";
});

describe("Dashboard", () => {
  it("is the Admin Home for administrators: real users, no invented figures", async () => {
    mockApi({
      "GET /api/v1/admin/users": { status: 200, body: { results: [makeAdminUser()], next: null, previous: null } },
    });
    renderWithProviders(<DashboardView />, { viewer: adminViewer });
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("No figures yet")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Rahul Sharma" })).toHaveAttribute(
      "href",
      `/admin/users/${RAHUL_ID}/dashboard`,
    );
    expect(screen.getByRole("link", { name: "Manage users" })).toHaveAttribute("href", "/admin/users");
    expect(document.body.textContent).not.toMatch(/₹|\d+ leads|pipeline value/i);
  });

  it("is an honest empty state for sales users (their dashboard arrives with Phase 5)", () => {
    const api = mockApi({});
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(screen.getByText("Dashboard isn't available yet")).toBeInTheDocument();
    expect(api.calls).toHaveLength(0); // no admin data is even requested
  });
});

describe("modules in another user's workspace", () => {
  it("say plainly that nothing is available yet, without fake data", () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/activities`;
    renderWithProviders(<ActivitiesView />, { viewer: adminViewer });
    expect(screen.getByText("Selected user's records")).toBeInTheDocument();
    expect(screen.getByText("This user's activities will appear here once the module is enabled.")).toBeInTheDocument();
  });

  it("the Pipeline (Phase 3) loads that user's board, scoped to their workspace", async () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    const api = mockApi({
      "GET /api/v1/config/pipelines": { status: 200, body: PIPELINES },
      [`GET /api/v1/workspaces/${RAHUL_ID}/pipeline-board`]: { status: 200, body: makeBoard() },
    });
    renderWithProviders(<PipelineView />, { viewer: adminViewer });
    expect(await screen.findByText("Hospital Analyzer Project")).toBeInTheDocument();
    expect(screen.getByText(/Selected user's records/)).toBeInTheDocument();
    expect(api.calls.every((c) => !c.path.includes("/workspaces/all") && !c.path.includes("/workspaces/me"))).toBe(true);
  });
});

describe("UserWorkspaceFrame", () => {
  it("names whose CRM the admin is viewing (the API audits the access)", async () => {
    const api = mockApi({
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: {
        status: 200,
        body: { kind: "user", subject: { id: RAHUL_ID, full_name: "Rahul Sharma", status: "active" } },
      },
    });
    renderWithProviders(<UserWorkspaceFrame userId={RAHUL_ID}>Workspace body</UserWorkspaceFrame>, {
      viewer: adminViewer,
    });
    expect(screen.getByText("Loading user name")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "Workspace context" })).toHaveTextContent(
        "Viewing CRM for: Rahul Sharma",
      ),
    );
    expect(screen.getByText("Workspace body")).toBeInTheDocument();
    expect(api.callsTo("GET", `/api/v1/workspaces/${RAHUL_ID}`)).toHaveLength(1);
  });

  it("flags a deactivated user's workspace", async () => {
    mockApi({
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: {
        status: 200,
        body: { kind: "user", subject: { id: RAHUL_ID, full_name: "Rahul Sharma", status: "deactivated" } },
      },
    });
    renderWithProviders(<UserWorkspaceFrame userId={RAHUL_ID}>x</UserWorkspaceFrame>, { viewer: adminViewer });
    expect(await screen.findByText(/Deactivated user/)).toBeInTheDocument();
  });

  it("shows not-found when the API refuses (never reveals whether the user exists)", async () => {
    mockApi({ [`GET /api/v1/workspaces/${RAHUL_ID}`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<UserWorkspaceFrame userId={RAHUL_ID}>Workspace body</UserWorkspaceFrame>, {
      viewer: adminViewer,
    });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.queryByText("Workspace body")).not.toBeInTheDocument();
  });
});
