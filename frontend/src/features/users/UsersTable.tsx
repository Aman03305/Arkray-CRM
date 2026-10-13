"use client";

import Link from "next/link";

import { ActionMenu, type MenuAction } from "@/components/ui/ActionMenu";
import { Badge } from "@/components/ui/Badge";
import { Skeleton } from "@/components/ui/Skeleton";
import type { AdminUser } from "@/lib/api/types";
import { formatDate, formatDateTime, formatRelative } from "@/lib/format";
import { canManageAdmins, canOpenUserWorkspaces } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { userWorkspaceHref } from "@/lib/workspace";

import { isAdministratorRole } from "./api";
import { UserStatus } from "./UserStatus";

export type UserAction =
  | "edit"
  | "change-email"
  | "resend"
  | "reset-mfa"
  | "clear-sign-in-lock"
  | "deactivate"
  | "activate";

export function actionsFor(user: AdminUser, isSelf: boolean): UserAction[] {
  // A pseudonymised former user is never changed again (review of 73ff1e3, L4; the API refuses).
  if (user.pseudonymised_at) return [];
  const actions: UserAction[] = ["edit"];
  // Another administrator (a Super admin or an Admin, ADR-0035) changes their own email in
  // Settings (the API refuses it).
  const administrator = isAdministratorRole(user.role);
  if (isSelf || !administrator) actions.push("change-email");
  if (user.status === "invited") actions.push("resend");
  // Never an administrator's (ADR-0033: an operator does that on the server); the API refuses it.
  if (user.mfa_enabled && !administrator && !isSelf) actions.push("reset-mfa");
  // Too many failed sign-ins locked it from new browsers (review of 73ff1e3, L1).
  if (user.sign_in_locked_at) actions.push("clear-sign-in-lock");
  if (user.status === "deactivated") actions.push("activate");
  else if (!isSelf) actions.push("deactivate"); // the API refuses self-deactivation too
  return actions;
}

const ACTION_LABELS: Record<UserAction, { label: string; tone?: "danger" }> = {
  edit: { label: "Edit details" },
  "change-email": { label: "Change email" },
  resend: { label: "Resend invitation" },
  "reset-mfa": { label: "Reset two-step verification" },
  "clear-sign-in-lock": { label: "Clear sign-in lock" },
  activate: { label: "Reactivate" },
  deactivate: { label: "Deactivate", tone: "danger" },
};

const COLUMNS = ["Name", "Email", "Role", "Status", "Two-step", "Last login", "Created"] as const;
// A Super admin also sees whose team each user is in (ADR-0035); an Admin's list is their team.
const COLUMNS_WITH_ADMIN = ["Name", "Email", "Role", "Admin", "Status", "Two-step", "Last login", "Created"] as const;

/** Whether two-step verification is on. */
export function TwoStepBadge({ user }: { user: AdminUser }) {
  return user.mfa_enabled ? <Badge tone="green">On</Badge> : <Badge>Off</Badge>;
}

interface UsersTableProps {
  users: readonly AdminUser[] | undefined;
  loading: boolean;
  viewerId: string | undefined;
  onAction: (action: UserAction, user: AdminUser) => void;
  /** A name opens the user's details (their CRM is the separate "Open CRM" link). */
  onOpen: (user: AdminUser) => void;
}

export function UsersTable({ users, loading, viewerId, onAction, onOpen }: UsersTableProps) {
  const viewer = useViewer();
  // Only viewers who may open these users' workspaces get the link (it would be "not found"):
  // anyone's, or a team administrator's team's (who are all they list).
  const opensCrm = canOpenUserWorkspaces(viewer);
  const showsManager = canManageAdmins(viewer);
  const columns = showsManager ? COLUMNS_WITH_ADMIN : COLUMNS;
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="min-w-full divide-y divide-slate-200 text-sm" aria-busy={loading || undefined}>
        <caption className="sr-only">Users{loading ? " (loading)" : ""}</caption>
        <thead className="bg-slate-50">
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col" className="whitespace-nowrap px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                {column}
              </th>
            ))}
            <th scope="col" className="px-4 py-2.5 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
              Actions
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {loading
            ? Array.from({ length: 5 }, (_, i) => (
                <tr key={i}>
                  {[...columns, "actions"].map((column) => (
                    <td key={column} className="px-4 py-3">
                      <Skeleton className="h-4 w-24" />
                    </td>
                  ))}
                </tr>
              ))
            : users?.map((user) => {
                const isSelf = user.id === viewerId;
                const actions: MenuAction[] = actionsFor(user, isSelf).map((action) => ({
                  key: action,
                  label: ACTION_LABELS[action].label,
                  tone: ACTION_LABELS[action].tone,
                  onSelect: () => onAction(action, user),
                }));
                return (
                  <tr key={user.id} className="hover:bg-slate-50/60">
                    <td className="whitespace-nowrap px-4 py-3">
                      <button
                        type="button"
                        aria-haspopup="dialog"
                        onClick={() => onOpen(user)}
                        className="rounded-sm text-left font-medium text-slate-900 hover:text-brand-700 hover:underline"
                      >
                        {user.full_name}
                      </button>
                      {isSelf ? <span className="ml-1.5 text-xs text-slate-500">(you)</span> : null}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">{user.email}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">{user.role_label}</td>
                    {showsManager ? (
                      <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                        {user.manager ? user.manager.full_name : <span className="text-slate-500">—</span>}
                      </td>
                    ) : null}
                    <td className="px-4 py-3">
                      <UserStatus user={user} />
                    </td>
                    <td className="whitespace-nowrap px-4 py-3">
                      <TwoStepBadge user={user} />
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                      {user.last_login ? (
                        <time dateTime={user.last_login} title={formatDateTime(user.last_login)}>
                          {formatRelative(user.last_login)}
                        </time>
                      ) : (
                        <span className="text-slate-500">Never</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                      <time dateTime={user.created_at} title={formatDateTime(user.created_at)}>
                        {formatDate(user.created_at)}
                      </time>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-2">
                        {opensCrm ? (
                          <Link
                            href={userWorkspaceHref(user.id)}
                            aria-label={`Open CRM for ${user.full_name}`}
                            className="whitespace-nowrap rounded-sm text-sm font-medium text-brand-700 hover:underline"
                          >
                            Open CRM
                          </Link>
                        ) : null}
                        <ActionMenu label={`Actions for ${user.full_name}`} actions={actions} />
                      </div>
                    </td>
                  </tr>
                );
              })}
        </tbody>
      </table>
    </div>
  );
}
