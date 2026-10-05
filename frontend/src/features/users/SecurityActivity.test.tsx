import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { SecurityEvent } from "@/lib/api/types";
import { adminViewer, makeViewer, RAHUL_ID } from "@/test/fixtures";
import { apiError, mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

import { AdminHome } from "./AdminHome";
import { generatePassword } from "./password";
import { SecurityActivity } from "./SecurityActivity";
import { describeSecurityEvent } from "./security-events";

const EVENTS = "GET /api/v1/admin/security-events";
const ANITA = { id: "8c11b60d-58f6-4eff-b571-8647cde4717e", full_name: "Anita Rao" };
const RAHUL = { id: RAHUL_ID, full_name: "Rahul Sharma" };

function event(action: string, overrides: Partial<SecurityEvent> = {}): SecurityEvent {
  return {
    id: `${action}-${Math.random()}`,
    action,
    occurred_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    actor: ANITA,
    user: RAHUL,
    details: {},
    in_support_session: false,
    ...overrides,
  };
}

function page(results: SecurityEvent[], next: string | null = null) {
  return { status: 200, body: { results, next, previous: null } };
}

describe("describeSecurityEvent", () => {
  it.each([
    [event("auth.password_changed", { actor: RAHUL }), "Rahul Sharma changed their password"],
    [event("auth.password_reset_completed", { actor: null }), "Rahul Sharma reset their password"],
    [event("auth.password_set_by_admin"), "Anita Rao set a new password for Rahul Sharma"],
    [event("user.created", { details: { role: "sales_user", activation: "password" } }), "Anita Rao created Rahul Sharma"],
    [event("user.deactivated"), "Anita Rao deactivated Rahul Sharma"],
    [event("user.reactivated"), "Anita Rao reactivated Rahul Sharma"],
    [event("user.role_changed", { details: { from: "sales_user", to: "admin" } }), "Anita Rao changed Rahul Sharma's role to Admin"],
    [event("user.email_changed"), "Anita Rao changed Rahul Sharma's email"],
    [event("support_session.started", { details: { reason: "Fixing a lead" } }), "Anita Rao started a support session for Rahul Sharma"],
    [event("support_session.ended", { details: { end: "expired" } }), "Anita Rao's support session for Rahul Sharma ended (expired)"],
    [event("support_session.ended", { details: { end: "exited" } }), "Anita Rao's support session for Rahul Sharma ended"],
    [event("support_session.ended", { actor: null, details: { end: "expired" } }), "Support session for Rahul Sharma ended (expired)"],
    [event("something.new"), "Account activity for Rahul Sharma"],
  ])("%#: %s", (input, sentence) => {
    expect(describeSecurityEvent(input)).toBe(sentence);
  });
});

describe("SecurityActivity", () => {
  it("lists recent events one line each, then pages on with Show more", async () => {
    const api = mockApi({
      [EVENTS]: (call: RecordedCall) =>
        call.query.get("cursor") === "p2"
          ? page([event("user.deactivated")])
          : page(
              [
                event("auth.password_changed", { actor: RAHUL }),
                event("support_session.started", { in_support_session: false }),
              ],
              "http://backend:8000/api/v1/admin/security-events?cursor=p2",
            ),
    });
    renderWithProviders(<SecurityActivity />, { viewer: adminViewer });
    const section = screen.getByRole("region", { name: "Security activity" });
    const first = await within(section).findByText(/Rahul Sharma changed their password/);
    expect(first.closest("li")).toHaveTextContent("Rahul Sharma changed their password · 5 minutes ago");
    expect(section).toHaveTextContent("Anita Rao started a support session for Rahul Sharma");
    expect(api.calls[0]!.query.get("page_size")).toBe("8");

    await userEvent.setup().click(within(section).getByRole("button", { name: "Show more" }));
    expect(await within(section).findByText(/Anita Rao deactivated Rahul Sharma/)).toBeInTheDocument();
    expect(within(section).getAllByRole("listitem")).toHaveLength(3);
    expect(within(section).queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    expect(api.fetchMock.mock.calls.every(([url]) => String(url).startsWith("/api/"))).toBe(true);
  });

  it("marks what happened during a support session", async () => {
    mockApi({ [EVENTS]: page([event("user.email_changed", { in_support_session: true })]) });
    renderWithProviders(<SecurityActivity />, { viewer: adminViewer });
    expect(await screen.findByText(/changed Rahul Sharma's email/)).toHaveTextContent("(in a support session)");
  });

  it("has an empty state and a retry", async () => {
    let attempts = 0;
    mockApi({
      [EVENTS]: () => {
        attempts += 1;
        return attempts === 1 ? apiError(503, "service_unavailable", "Down.") : page([]);
      },
    });
    renderWithProviders(<SecurityActivity />, { viewer: adminViewer });
    const alert = await screen.findByRole("alert");
    await userEvent.setup().click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No security activity yet.")).toBeInTheDocument();
  });

  it("is on the organisation dashboard only for viewers who may read the security log", async () => {
    mockApi({ [EVENTS]: page([]) });
    const { unmount } = renderWithProviders(<AdminHome />, { viewer: adminViewer });
    expect(screen.getByRole("region", { name: "Security activity" })).toBeInTheDocument();
    unmount();

    const api = mockApi({ [EVENTS]: page([]) });
    renderWithProviders(<AdminHome />, {
      viewer: makeViewer({ capabilities: ["crm.access_own", "crm.view_all"] }),
    });
    expect(screen.queryByRole("region", { name: "Security activity" })).not.toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/admin/security-events")).toHaveLength(0);
  });
});

describe("generatePassword", () => {
  it("makes long, readable, different passwords from unambiguous characters", () => {
    const made = Array.from({ length: 50 }, generatePassword);
    for (const password of made) {
      expect(password).toMatch(/^[a-hjkmnp-zA-HJ-NP-Z2-9]{4}(-[a-hjkmnp-zA-HJ-NP-Z2-9]{4}){3}$/);
      expect(password).not.toMatch(/[0O1lIio]/);
    }
    expect(new Set(made).size).toBe(made.length);
  });
});
