"use client";

import Link from "next/link";

import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { userWorkspaceHref } from "@/lib/workspace";

/**
 * A user's name, opening their CRM workspace (their Dashboard). Editing the user is a
 * separate, explicit action. Only viewers who may open any user's workspace
 * (workspace.view_any) get a link; for anyone else the name is plain text, because the link
 * would only lead to "not found" (the API decides either way).
 */
export function UserWorkspaceLink({
  user,
  className = "",
}: {
  user: { id: string; full_name: string };
  className?: string;
}) {
  if (!hasCapability(useViewer(), "workspace.view_any")) {
    return <span className={`font-medium text-slate-900 ${className}`}>{user.full_name}</span>;
  }
  return (
    <Link
      href={userWorkspaceHref(user.id)}
      aria-label={`${user.full_name}, open CRM workspace`}
      className={`font-medium text-slate-900 hover:text-brand-700 hover:underline ${className}`}
    >
      {user.full_name}
    </Link>
  );
}
