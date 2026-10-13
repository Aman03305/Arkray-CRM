import type { AdminUser, OpportunityOptions } from "@/lib/api/types";
import type { Capability, Viewer } from "@/lib/viewer";

export const RAHUL_ID = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";

const ADMIN_CAPABILITIES: Capability[] = [
  "ai.query",
  "audit.view",
  "config.manage",
  "crm.access_own",
  "crm.assign_any",
  "crm.manage_any",
  "crm.view_all",
  "privacy.manage",
  "support.access",
  "users.manage",
  "workspace.view_any",
];

export function makeViewer(overrides: Partial<Viewer> = {}): Viewer {
  const firstName = overrides.firstName ?? "Priya";
  const lastName = overrides.lastName ?? "Patel";
  return {
    id: "u1",
    email: "priya@example.test",
    firstName,
    lastName,
    fullName: `${firstName} ${lastName}`.trim(),
    roleLabel: "User",
    capabilities: ["crm.access_own", "ai.query"],
    features: { ask: false },
    passwordChangeRequired: false,
    mfaEnabled: false,
    mfaEnrollmentRequired: false,
    supportSession: null,
    ...overrides,
  };
}

// A team administrator (ADR-0035): the team counterparts, never an organisation-wide capability.
export const TEAM_ADMIN_CAPABILITIES: Capability[] = [
  "ai.query",
  "crm.access_own",
  "crm.assign_team",
  "crm.manage_team",
  "crm.view_team",
  "support.access_team",
  "users.manage_team",
  "workspace.view_team",
];

export const salesViewer = makeViewer();
export const adminViewer = makeViewer({
  id: "a1",
  email: "admin@example.test",
  firstName: "Anita",
  lastName: "Admin",
  fullName: "Anita Admin",
  // The stored role "admin" is labelled "Super admin" since ADR-0035.
  roleLabel: "Super admin",
  capabilities: ADMIN_CAPABILITIES,
});
/** Tara, a team administrator ("Admin"; the super administrator above is adminViewer). */
export const teamAdminViewer = makeViewer({
  id: "7a1d2c3b-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
  email: "tara@example.test",
  firstName: "Tara",
  lastName: "Singh",
  fullName: "Tara Singh",
  roleLabel: "Admin",
  capabilities: TEAM_ADMIN_CAPABILITIES,
});

export function makeAdminUser(overrides: Partial<AdminUser> = {}): AdminUser {
  const base: AdminUser = {
    id: RAHUL_ID,
    email: "rahul@example.test",
    first_name: "Rahul",
    last_name: "Sharma",
    full_name: "Rahul Sharma",
    role: "sales_user",
    role_label: "User",
    status: "active",
    status_label: "Active",
    last_login: "2026-09-29T10:15:00Z",
    created_at: "2026-09-01T04:30:00Z",
    activated_at: "2026-09-01T05:00:00Z",
    deactivated_at: null,
    invitation: null,
    password_change_required: false,
    password_changed_at: "2026-09-01T05:00:00Z",
    mfa_enabled: false,
    mfa_lock: null,
    sign_in_locked_at: null,
    pseudonymised_at: null,
    manager: null,
    version: 1,
  };
  return { ...base, ...overrides };
}

// --- leads (made with their opportunity; no Leads module, ADR-0027; a read-only page, ADR-0028)
export const LEAD_ID = "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c";
export const PRIYA_ID = "5c4b3a29-1807-4f6e-9d5c-4b3a29180716";

// --- the New Opportunity form's choices (GET /api/v1/config/opportunity-options) ----------
export const INSTRUMENT_NAMES = ["Adams 8380 V-lite", "Adams 8180 V", "Adams 8180 T", "PCBA with Printer"] as const;
export const OPPORTUNITY_OPTIONS: OpportunityOptions = { instruments: INSTRUMENT_NAMES.map((name) => ({ name })) };
/** A mockApi route answering the instrument list (shared configuration, not a workspace's). */
export const OPPORTUNITY_OPTIONS_ROUTE = {
  "GET /api/v1/config/opportunity-options": { status: 200, body: OPPORTUNITY_OPTIONS },
};
