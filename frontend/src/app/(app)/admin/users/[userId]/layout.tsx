import { notFound } from "next/navigation";

import { RequireCapability } from "@/features/auth/RequireCapability";
import { UserWorkspaceFrame } from "@/features/workspace/UserWorkspaceFrame";
import { isUuid } from "@/lib/workspace";

/*
 * An admin viewing one user's CRM. Every page below renders the same module views as the
 * user's own routes; the views scope their API calls to /api/v1/workspaces/{userId}/...,
 * which the backend authorises (and audits) on every request.
 */
export default async function UserWorkspaceLayout({ children, params }: LayoutProps<"/admin/users/[userId]">) {
  const { userId } = await params;
  if (!isUuid(userId)) notFound();
  return (
    <RequireCapability capability="workspace.view_any">
      <UserWorkspaceFrame userId={userId.toLowerCase()}>{children}</UserWorkspaceFrame>
    </RequireCapability>
  );
}
