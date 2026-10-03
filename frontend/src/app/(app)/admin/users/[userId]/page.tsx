import { notFound, redirect } from "next/navigation";

import { isUuid, userWorkspaceHref } from "@/lib/workspace";

/* /admin/users/{id} is the user's workspace: it opens on their Dashboard. Only a user id is
   ever put into the redirect (anything else is "not found", like the layout). */
export default async function UserWorkspaceIndex({ params }: PageProps<"/admin/users/[userId]">) {
  const { userId } = await params;
  if (!isUuid(userId)) notFound();
  redirect(userWorkspaceHref(userId));
}
