import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api/client";
import type {
  AdminUser,
  AdminUserPage,
  DeleteAccountRequest,
  DeletionPreview,
  DeletionResult,
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
/** What deleting one account would do (under USERS_QUERY_KEY, so a refresh of the users reads it again). */
export const deletionPreviewKey = (id: string) => [...USERS_QUERY_KEY, "deletion-preview", id] as const;
export const SECURITY_EVENTS_QUERY_KEY = ["security-events"] as const;
export const SEARCH_MIN_LENGTH = 2;

export interface UserFilters {
  q: string;
  status: UserStatus | "";
  role: Role | "";
  /** Whose team (ADR-0035): an Admin's id, NO_MANAGER for users without one, or "" for any. */
  manager: string;
}

/** The `manager` filter's value for sales users without an Admin (the API's literal). */
export const NO_MANAGER = "none";

export const NO_FILTERS: UserFilters = { q: "", status: "", role: "", manager: "" };

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
  if (filters.manager) params.set("manager", filters.manager);
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
  /** Removes an ordinary user's two-step verification (ADR-0033; never an administrator's). */
  resetMfa: (id: string) => apiFetch<AdminUser>(user(id, "mfa/reset"), { method: "POST" }),
  /** Lifts the lock too many failed sign-ins put on new browsers (review of 73ff1e3, L1). */
  clearSignInLock: (id: string) => apiFetch<AdminUser>(user(id, "clear-sign-in-lock"), { method: "POST" }),
  /**
   * Emails the account a single-use reset link (ADR-0036): their old password stops working and
   * their sessions end. The administrator never sees or chooses a password.
   */
  resetPassword: (id: string) => apiFetch<AdminUser>(user(id, "reset-password"), { method: "POST" }),
  /** What deleting the account would do, and why it can't be done now, if so (ADR-0036). */
  deletionPreview: (id: string) => apiFetch<DeletionPreview>(user(id, "deletion-preview")),
  /**
   * Deletes the account (ADR-0036): 200 `completed` when done; 202 (`completed: false`) after
   * handing over a batch of leads, then the SAME request is sent again until it is 200.
   */
  deleteAccount: (id: string, body: DeleteAccountRequest) =>
    apiFetch<DeletionResult>(user(id, "delete"), { method: "POST", body }),
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

// The labels are the API's role_label (ADR-0035): `admin` is shown as "Super admin".
export const ROLE_OPTIONS: readonly { value: Role; label: string; description: string }[] = [
  { value: "sales_user", label: "User", description: "Works in their own CRM workspace." },
  {
    value: "team_admin",
    label: "Admin",
    description: "Manages the users in their team and can open their workspaces.",
  },
  {
    value: "admin",
    label: "Super admin",
    description: "Manages every user and Admin, the shared settings, and can open every user's workspace.",
  },
];

/**
 * Super admins and Admins: always invited (never given a password), and only they change
 * their own email, password and two-step verification (ADR-0030, ADR-0035). The account's
 * role, not the viewer's: the API refuses the same cases.
 */
export function isAdministratorRole(role: Role): boolean {
  return role === "admin" || role === "team_admin";
}

/**
 * Why a Super admin can't change this account's role (null when they can). The API refuses
 * the same cases: one's own role, and another Super admin's (ADR-0030, kept by ADR-0035:
 * deactivate instead).
 */
export function roleChangeBlockedReason(user: AdminUser, isSelf: boolean): string | null {
  if (isSelf) return "You can't change your own role.";
  if (user.role === "admin") return "A Super admin's role can't be changed. To remove their access, deactivate the account.";
  return null;
}

/**
 * The team administrators (Admins) a user can be given to, or listed by (ADR-0035): one page
 * of the API's list (newest first; `q` narrows it server-side), in the order to show them,
 * by name. `truncated`: there are more than this page holds, so the pickers offer a search.
 */
export const teamAdminsKey = (q: string) => [...USERS_QUERY_KEY, "team-admins", q] as const;
export const TEAM_ADMINS_PAGE_SIZE = 100;

const NAME_ORDER = new Intl.Collator("en", { sensitivity: "base" });

/** By name, then email (two Admins may share a name): the order every Admin picker shows. */
export function byName<T extends Pick<AdminUser, "full_name" | "email">>(users: readonly T[]): T[] {
  return [...users].sort((a, b) => NAME_ORDER.compare(a.full_name, b.full_name) || NAME_ORDER.compare(a.email, b.email));
}

export function useTeamAdmins(q: string, enabled: boolean) {
  const query = useQuery({
    queryKey: teamAdminsKey(q),
    queryFn: () => usersApi.list({ ...NO_FILTERS, q, role: "team_admin" }, null, TEAM_ADMINS_PAGE_SIZE),
    enabled,
    staleTime: 30_000,
  });
  return {
    query,
    admins: byName(query.data?.results ?? []),
    truncated: Boolean(query.data?.next) || q !== "",
  };
}

export const STATUS_OPTIONS: readonly { value: UserStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "invited", label: "Invited" },
  { value: "deactivated", label: "Deactivated" },
];
