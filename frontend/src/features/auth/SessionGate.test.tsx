import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@/lib/api/client";
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

describe("SessionGate: a password set by an administrator", () => {
  const TEMPORARY = "given-by-anita-42";
  const CHOSEN = "my own long passphrase";
  const MUST_CHANGE = { ...ME, password_change_required: true, support_session: null };

  beforeEach(() => nav.hardNavigate.mockReset());

  it("must be replaced before the app opens; then the app opens", async () => {
    let changed = false;
    const api = mockApi({
      "GET /api/v1/auth/me": () => ({ status: 200, body: changed ? { ...MUST_CHANGE, password_change_required: false } : MUST_CHANGE }),
      "POST /api/v1/auth/password/change": () => {
        changed = true;
        return { status: 204 };
      },
    });
    renderWithProviders(
      <SessionGate>
        <WhoAmI />
      </SessionGate>,
    );
    expect(await screen.findByRole("heading", { name: "Choose a new password" })).toBeInTheDocument();
    expect(screen.queryByText(/Signed in as/)).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Temporary password"), TEMPORARY);
    await user.type(screen.getByLabelText("New password"), CHOSEN);
    await user.type(screen.getByLabelText("Confirm password"), CHOSEN);
    await user.click(screen.getByRole("button", { name: "Change password" }));

    expect(await screen.findByText("Signed in as Rahul Sharma")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Choose a new password" })).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/auth/password/change")[0]!.body).toEqual({
      current_password: TEMPORARY,
      new_password: CHOSEN,
    });
    expect(document.body.innerHTML).not.toContain(CHOSEN);
  });

  it("puts the server's answers on their fields and keeps the app closed", async () => {
    mockApi({
      "GET /api/v1/auth/me": { status: 200, body: MUST_CHANGE },
      "POST /api/v1/auth/password/change": apiError(400, "validation_error", "Invalid.", {
        current_password: ["Your current password is incorrect."],
      }),
    });
    renderWithProviders(
      <SessionGate>
        <WhoAmI />
      </SessionGate>,
    );
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Temporary password"), "wrong");
    await user.type(screen.getByLabelText("New password"), CHOSEN);
    await user.type(screen.getByLabelText("Confirm password"), CHOSEN);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Temporary password")).toHaveAccessibleDescription("Your current password is incorrect."),
    );
    expect(screen.queryByText(/Signed in as/)).not.toBeInTheDocument();
  });

  it("checks the confirmation before sending", async () => {
    const api = mockApi({ "GET /api/v1/auth/me": { status: 200, body: MUST_CHANGE } });
    renderWithProviders(
      <SessionGate>
        <WhoAmI />
      </SessionGate>,
    );
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Temporary password"), TEMPORARY);
    await user.type(screen.getByLabelText("New password"), CHOSEN);
    await user.type(screen.getByLabelText("Confirm password"), "something else");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(screen.getByLabelText("Confirm password")).toHaveAccessibleDescription("The passwords don't match.");
    expect(api.callsTo("POST", "/api/v1/auth/password/change")).toHaveLength(0);
  });

  it("can sign out instead", async () => {
    const api = mockApi({
      "GET /api/v1/auth/me": { status: 200, body: MUST_CHANGE },
      "POST /api/v1/auth/logout": { status: 204 },
    });
    renderWithProviders(
      <SessionGate>
        <WhoAmI />
      </SessionGate>,
    );
    await userEvent.setup().click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(nav.hardNavigate).toHaveBeenCalledWith("/login?reason=signed-out", "signed-out"));
    expect(api.callsTo("POST", "/api/v1/auth/logout")).toHaveLength(1);
  });

  it("opens as soon as any request says a new password is required", async () => {
    let required = false;
    mockApi({
      "GET /api/v1/auth/me": () => ({ status: 200, body: { ...ME, password_change_required: required, support_session: null } }),
      "GET /api/v1/workspaces/me/leads": apiError(403, "password_change_required", "Choose a new password first."),
    });
    const client = createQueryClient();
    render(
      <QueryClientProvider client={client}>
        <SessionGate>
          <WhoAmI />
        </SessionGate>
      </QueryClientProvider>,
    );
    await screen.findByText("Signed in as Rahul Sharma");
    required = true; // an administrator set the password meanwhile
    await client.fetchQuery({ queryKey: ["leads"], queryFn: () => apiFetch("/api/v1/workspaces/me/leads") }).catch(() => undefined);
    expect(await screen.findByRole("heading", { name: "Choose a new password" })).toBeInTheDocument();
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
