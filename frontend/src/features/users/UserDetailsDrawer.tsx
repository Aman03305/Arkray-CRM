"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, KeyRound, LifeBuoy } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { describeError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";
import { formatDate, formatDateTime, formatRelative } from "@/lib/format";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { userWorkspaceHref } from "@/lib/workspace";

import { userDetailKey, usersApi } from "./api";
import { UserStatus } from "./UserStatus";

/** The password's state, never the password (the API has none to give). */
export function passwordState(user: AdminUser): string {
  if (user.status === "invited") return "Invitation pending";
  if (user.password_change_required) return "Must be changed at next sign-in";
  if (user.password_changed_at) return `Set ${formatDate(user.password_changed_at)}`;
  return "Not set";
}

/**
 * Why an administrator can't set this user's password or open a support session for them
 * (null when they can). The API refuses the same cases.
 */
export function supportBlockedReason(user: AdminUser, isSelf: boolean): string | null {
  if (isSelf) return "This is you. Change your own password in Settings.";
  if (user.role === "admin") return "Not available for administrators.";
  if (user.status === "invited") return "Not available until they accept their invitation.";
  if (user.status === "deactivated") return "Not available for deactivated users.";
  return null;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-3 py-2.5">
      <dt className="text-slate-500">{label}</dt>
      <dd className="min-w-0 text-slate-900 [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

/**
 * A user's details beside the Users list (their name opens it): who they are, their status,
 * and the administrator's sign-in tools for them. Their CRM is a separate, explicit link.
 */
export function UserDetailsDrawer({ user: listed, notice, onClose, onSetPassword, onAccess }: {
  /** As listed; the latest copy is loaded while the panel is open. */
  user: AdminUser;
  notice: string | null;
  onClose: () => void;
  onSetPassword: (user: AdminUser) => void;
  onAccess: (user: AdminUser) => void;
}) {
  const viewer = useViewer();
  const detail = useQuery({
    queryKey: userDetailKey(listed.id),
    queryFn: () => usersApi.get(listed.id),
    placeholderData: listed,
  });
  const user = detail.data ?? listed;
  const manages = hasCapability(viewer, "users.manage");
  const supports = hasCapability(viewer, "support.access");
  const opensCrm = hasCapability(viewer, "workspace.view_any");
  const blocked = supportBlockedReason(user, user.id === viewer?.id);
  const loadError = detail.isError ? describeError(detail.error) : null;

  return (
    <Drawer
      open
      title={user.full_name}
      description={user.email}
      onClose={onClose}
      width="md"
      footer={
        opensCrm ? (
          <Link
            href={userWorkspaceHref(user.id)}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-full border border-slate-300 bg-white px-3.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Open CRM
            <ArrowUpRight aria-hidden="true" className="size-4" />
          </Link>
        ) : undefined
      }
    >
      <div className="space-y-4">
        <div aria-live="polite" className="empty:hidden">
          {notice ? <Alert tone="success">{notice}</Alert> : null}
        </div>
        {loadError ? (
          <Alert tone="error" requestId={loadError.requestId}>
            The latest details couldn&apos;t be loaded. {loadError.message}
          </Alert>
        ) : null}
        <dl className="divide-y divide-slate-100 text-sm">
          <Row label="Role">{user.role_label}</Row>
          <Row label="Status">
            <UserStatus user={user} />
          </Row>
          <Row label="Last sign-in">
            {user.last_login ? (
              <time dateTime={user.last_login} title={formatDateTime(user.last_login)}>
                {formatRelative(user.last_login)}
              </time>
            ) : (
              "Never"
            )}
          </Row>
          <Row label="Created">
            <time dateTime={user.created_at} title={formatDateTime(user.created_at)}>
              {formatDate(user.created_at)}
            </time>
          </Row>
          <Row label="Password">
            {passwordState(user)}
            <span className="mt-0.5 block text-xs text-slate-500">Passwords are never shown.</span>
          </Row>
        </dl>
        {manages ? (
          <div className="border-t border-slate-200 pt-4">
            {blocked ? (
              <p className="text-sm text-slate-500">{blocked}</p>
            ) : (
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" icon={<KeyRound aria-hidden="true" className="size-4" />} onClick={() => onSetPassword(user)}>
                  Set new password
                </Button>
                {supports ? (
                  <Button variant="secondary" icon={<LifeBuoy aria-hidden="true" className="size-4" />} onClick={() => onAccess(user)}>
                    Access as user
                  </Button>
                ) : null}
              </div>
            )}
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}
