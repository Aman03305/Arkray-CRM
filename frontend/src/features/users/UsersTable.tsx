"use client";

import Link from "next/link";

import { ActionMenu, type MenuAction } from "@/components/ui/ActionMenu";
import { Skeleton } from "@/components/ui/Skeleton";
import type { AdminUser } from "@/lib/api/types";
import { formatDate, formatDateTime, formatRelative } from "@/lib/format";
import { workspaceHref } from "@/lib/workspace";

import { UserStatus } from "./UserStatus";

export type UserAction = "edit" | "change-email" | "resend" | "deactivate" | "activate";

export function actionsFor(user: AdminUser, isSelf: boolean): UserAction[] {
  const actions: UserAction[] = ["edit", "change-email"];
  if (user.status === "invited") actions.push("resend");
  if (user.status === "deactivated") actions.push("activate");
  else if (!isSelf) actions.push("deactivate"); // the API refuses self-deactivation too
  return actions;
}

const ACTION_LABELS: Record<UserAction, { label: string; tone?: "danger" }> = {
  edit: { label: "Edit details" },
  "change-email": { label: "Change email" },
  resend: { label: "Resend invitation" },
  activate: { label: "Reactivate" },
  deactivate: { label: "Deactivate", tone: "danger" },
};

const COLUMNS = ["Name", "Email", "Role", "Status", "Last login", "Created"] as const;

interface UsersTableProps {
  users: readonly AdminUser[] | undefined;
  loading: boolean;
  viewerId: string | undefined;
  onAction: (action: UserAction, user: AdminUser) => void;
}

export function UsersTable({ users, loading, viewerId, onAction }: UsersTableProps) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="min-w-full divide-y divide-slate-200 text-sm" aria-busy={loading || undefined}>
        <caption className="sr-only">Users{loading ? " (loading)" : ""}</caption>
        <thead className="bg-slate-50">
          <tr>
            {COLUMNS.map((column) => (
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
                  {[...COLUMNS, "actions"].map((column) => (
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
                      <Link
                        href={workspaceHref({ kind: "user", userId: user.id }, "dashboard")}
                        className="font-medium text-slate-900 hover:text-brand-700 hover:underline"
                      >
                        {user.full_name}
                      </Link>
                      {isSelf ? <span className="ml-1.5 text-xs text-slate-400">(you)</span> : null}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">{user.email}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">{user.role_label}</td>
                    <td className="px-4 py-3">
                      <UserStatus user={user} />
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                      {user.last_login ? (
                        <time dateTime={user.last_login} title={formatDateTime(user.last_login)}>
                          {formatRelative(user.last_login)}
                        </time>
                      ) : (
                        <span className="text-slate-400">Never</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                      <time dateTime={user.created_at} title={formatDateTime(user.created_at)}>
                        {formatDate(user.created_at)}
                      </time>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <ActionMenu label={`Actions for ${user.full_name}`} actions={actions} />
                    </td>
                  </tr>
                );
              })}
        </tbody>
      </table>
    </div>
  );
}
