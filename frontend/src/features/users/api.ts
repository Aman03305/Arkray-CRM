import { apiFetch } from "@/lib/api/client";
import type {
  AdminUser,
  AdminUserPage,
  EmailChangeRequest,
  Role,
  SecurityEventPage,
  SetPasswordRequest,
  SupportSessionDto,
  SupportSessionStartRequest,
  UserCreateRequest,
  UserStatus,
  UserUpdateRequest,
} from "@/lib/api/types";
import type { SupportSession } from "@/lib/viewer";

const USERS = "/api/v1/admin/users";
export const USERS_QUERY_KEY = ["admin-users"] as const;
/** One user, freshly loaded (under USERS_QUERY_KEY, so list refreshes include it). */
export const userDetailKey = (id: string) => [...USERS_QUERY_KEY, "detail", id] as const;
export const SECURITY_EVENTS_QUERY_KEY = ["security-events"] as const;
export const SEARCH_MIN_LENGTH = 2;

export interface UserFilters {
  q: string;
  status: UserStatus | "";
  role: Role | "";
}

export const NO_FILTERS: UserFilters = { q: "", status: "", role: "" };

/** The cursor inside a `next`/`previous` link. Only the cursor is used: the link's host
 * is whatever the API saw, and the client only ever calls its own origin. */
export function cursorOf(link: string | null | undefined): string | null {
  if (!link) return null;
  try {
    return new URL(link, "https://arkray.invalid").searchParams.get("cursor");
  } catch {
    return null;
  }
}

export function listPath(filters: UserFilters, cursor: string | null, pageSize = 25): string {
  const params = new URLSearchParams();
  const q = filters.q.trim();
  if (q.length >= SEARCH_MIN_LENGTH) params.set("q", q);
  if (filters.status) params.set("status", filters.status);
  if (filters.role) params.set("role", filters.role);
  if (cursor) params.set("cursor", cursor);
  params.set("page_size", String(pageSize));
  return `${USERS}?${params.toString()}`;
}

const user = (id: string, action = "") => `${USERS}/${encodeURIComponent(id)}${action ? `/${action}` : ""}`;

export const usersApi = {
  list: (filters: UserFilters, cursor: string | null, pageSize?: number) =>
    apiFetch<AdminUserPage>(listPath(filters, cursor, pageSize)),
  create: (body: UserCreateRequest) => apiFetch<AdminUser>(USERS, { method: "POST", body }),
  update: (id: string, body: UserUpdateRequest) => apiFetch<AdminUser>(user(id), { method: "PATCH", body }),
  changeEmail: (id: string, body: EmailChangeRequest) =>
    apiFetch<AdminUser>(user(id, "change-email"), { method: "POST", body }),
  deactivate: (id: string) => apiFetch<AdminUser>(user(id, "deactivate"), { method: "POST" }),
  activate: (id: string) => apiFetch<AdminUser>(user(id, "activate"), { method: "POST" }),
  resendInvitation: (id: string) => apiFetch<AdminUser>(user(id, "resend-invitation"), { method: "POST" }),
  get: (id: string) => apiFetch<AdminUser>(user(id)),
  /** The user must replace it at their next sign-in; their sessions end. Never returned. */
  setPassword: (id: string, body: SetPasswordRequest) =>
    apiFetch<AdminUser>(user(id, "set-password"), { method: "POST", body }),
};

const SUPPORT_SESSIONS = "/api/v1/admin/support-sessions";

export const supportApi = {
  start: (body: SupportSessionStartRequest) =>
    apiFetch<SupportSessionDto>(SUPPORT_SESSIONS, { method: "POST", body }),
  /** Idempotent: ending a session that is already over is fine. */
  exit: () => apiFetch<void>(`${SUPPORT_SESSIONS}/current`, { method: "DELETE" }),
};

/** The API's support session in the shape the viewer carries it (lib/viewer). */
export function toSupportSession(dto: SupportSessionDto): SupportSession {
  return {
    id: dto.id,
    target: { id: dto.target.id.toLowerCase(), fullName: dto.target.full_name },
    reason: dto.reason,
    startedAt: dto.started_at,
    expiresAt: dto.expires_at,
  };
}

export const SECURITY_EVENTS_PAGE_SIZE = 8;

export const securityApi = {
  events: (cursor: string | null, pageSize = SECURITY_EVENTS_PAGE_SIZE) => {
    const params = new URLSearchParams({ page_size: String(pageSize) });
    if (cursor) params.set("cursor", cursor);
    return apiFetch<SecurityEventPage>(`/api/v1/admin/security-events?${params.toString()}`);
  },
};

export const ROLE_OPTIONS: readonly { value: Role; label: string; description: string }[] = [
  { value: "sales_user", label: "User", description: "Works in their own CRM workspace." },
  { value: "admin", label: "Admin", description: "Manages users and can open every user's workspace." },
];

export const STATUS_OPTIONS: readonly { value: UserStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "invited", label: "Invited" },
  { value: "deactivated", label: "Deactivated" },
];
