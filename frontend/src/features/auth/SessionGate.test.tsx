import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/lib/query-client";
import { useViewer } from "@/lib/viewer-context";
import { adminViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { RequireCapability } from "./RequireCapability";
import { SessionGate } from "./SessionGate";

const nav = vi.hoisted(() => ({ hardNavigate: vi.fn() }));
vi.mock("@/lib/browser", () => ({ hardNavigate: nav.hardNavigate, currentLocation: () => "/leads" }));

function WhoAmI() {
  const viewer = useViewer();
  return <p>{viewer ? `Signed in as ${viewer.fullName}` : "Loading viewer"}</p>;
}

const ME = {
  id: "u1",
  email: "rahul@example.test",
  first_name: "Rahul",
  last_name: "Sharma",
  full_name: "Rahul Sharma",
  role: "sales_user",
  role_label: "User",
  capabilities: ["crm.access_own"],
};

describe("SessionGate", () => {
  beforeEach(() => {
    nav.hardNavigate.mockReset();
    window.history.replaceState(null, "", "/leads");
  });

  it("provides the signed-in user", async () => {
    mockApi({ "GET /api/v1/auth/me": { status: 200, body: ME } });
    renderWithProviders(
      <SessionGate>
        <WhoAmI />
      </SessionGate>,
    );
    expect(screen.getByText("Loading viewer")).toBeInTheDocument();
    expect(await screen.findByText("Signed in as Rahul Sharma")).toBeInTheDocument();
  });

  it("sends visitors without a session to sign in, and back here afterwards", async () => {
    mockApi({ "GET /api/v1/auth/me": apiError(401, "not_authenticated", "Authentication credentials were not provided.") });
    render(
      <QueryClientProvider client={createQueryClient()}>
        <SessionGate>
          <WhoAmI />
        </SessionGate>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/login?next=%2Fleads", undefined));
    expect(screen.getByText("Loading viewer")).toBeInTheDocument(); // never guessed data
  });

  it("offers a retry when the account can't be loaded", async () => {
    let attempts = 0;
    mockApi({
      "GET /api/v1/auth/me": () => {
        attempts += 1;
        return attempts === 1 ? apiError(503, "service_unavailable", "Down.") : { status: 200, body: ME };
      },
    });
    renderWithProviders(
      <SessionGate>
        <WhoAmI />
      </SessionGate>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("We couldn't load your account");
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Signed in as Rahul Sharma")).toBeInTheDocument();
  });
});

describe("RequireCapability", () => {
  it("shows its content to viewers holding the capability", () => {
    renderWithProviders(<RequireCapability capability="users.manage">Admin area</RequireCapability>, {
      viewer: adminViewer,
    });
    expect(screen.getByText("Admin area")).toBeInTheDocument();
  });

  it("shows everyone else the standard not-found page", async () => {
    const { makeViewer } = await import("@/test/fixtures");
    renderWithProviders(<RequireCapability capability="users.manage">Admin area</RequireCapability>, {
      viewer: makeViewer(),
    });
    expect(screen.queryByText("Admin area")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("waits for the viewer instead of guessing", () => {
    renderWithProviders(<RequireCapability capability="users.manage">Admin area</RequireCapability>);
    expect(screen.queryByText("Admin area")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Page not found" })).not.toBeInTheDocument();
  });
});
