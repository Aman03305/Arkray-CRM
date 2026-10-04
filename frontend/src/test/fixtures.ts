import type { AdminUser, Lead, LeadListItem, LeadOptions } from "@/lib/api/types";
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
    ...overrides,
  };
}

export const salesViewer = makeViewer();
export const adminViewer = makeViewer({
  id: "a1",
  email: "admin@example.test",
  firstName: "Anita",
  lastName: "Admin",
  fullName: "Anita Admin",
  roleLabel: "Admin",
  capabilities: ADMIN_CAPABILITIES,
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
    version: 1,
  };
  return { ...base, ...overrides };
}

// --- leads ---------------------------------------------------------------------------------
export const LEAD_ID = "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c";
export const PRIYA_ID = "5c4b3a29-1807-4f6e-9d5c-4b3a29180716";

export function makeLead(overrides: Partial<Lead> = {}): Lead {
  const base: Lead = {
    id: LEAD_ID,
    display_name: "Asha Mehta",
    first_name: "Asha",
    last_name: "Mehta",
    organization_name: "Apollo Diagnostics",
    job_title: "Lab Director",
    email: "asha@apollo.example",
    phone: "+91 98765 43210",
    mobile: "",
    alternate_phone: "",
    status: { key: "new", name: "New", category: "open" },
    source: { key: "referral", name: "Referral" },
    rating: "hot",
    owner: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true },
    created_by: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true },
    last_contacted_at: null,
    archived_at: null,
    created_at: "2026-09-20T04:30:00Z",
    updated_at: "2026-09-21T04:30:00Z",
    version: 3,
    address_line_1: "",
    address_line_2: "",
    city: "Mumbai",
    state: "",
    postal_code: "",
    country: "IN",
    description: "",
  };
  return { ...base, ...overrides };
}

export function makeLeadListItem(overrides: Partial<LeadListItem> = {}): LeadListItem {
  const { id, display_name, first_name, last_name, organization_name, job_title, email, phone, mobile, status, source, rating, owner, last_contacted_at, archived_at, created_at, updated_at, version } = makeLead();
  return {
    id, display_name, first_name, last_name, organization_name, job_title, email, phone, mobile, status, source, rating,
    owner, last_contacted_at, archived_at, created_at, updated_at, version,
    ...overrides,
  };
}

export const LEAD_OPTIONS: LeadOptions = {
  statuses: [
    { key: "new", name: "New", category: "open", is_active: true, is_default: true },
    { key: "contacted", name: "Contacted", category: "open", is_active: true, is_default: false },
    { key: "qualified", name: "Qualified", category: "qualified", is_active: true, is_default: false },
    { key: "unqualified", name: "Unqualified", category: "unqualified", is_active: true, is_default: false },
    { key: "converted", name: "Converted", category: "converted", is_active: true, is_default: false },
  ],
  sources: [
    { key: "website", name: "Website", is_active: true },
    { key: "referral", name: "Referral", is_active: true },
    { key: "event", name: "Event", is_active: false },
  ],
  ratings: [
    { key: "hot", name: "Hot" },
    { key: "warm", name: "Warm" },
    { key: "cold", name: "Cold" },
  ],
  countries: ["GB", "IN", "US"],
};
