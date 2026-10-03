import { redirect } from "next/navigation";

export default async function UserWorkspaceIndex({ params }: PageProps<"/admin/users/[userId]">) {
  const { userId } = await params;
  redirect(`/admin/users/${userId}/dashboard`);
}
