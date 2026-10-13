import type { Metadata } from "next";

import { RequireCapability } from "@/features/auth/RequireCapability";
import { UsersPage } from "@/features/users/UsersPage";

// "Users" for a Super admin, "My team" for an Admin (ADR-0035): the tab title fits both.
export const metadata: Metadata = { title: "Users and teams" };

export default function Page() {
  return (
    <RequireCapability capability={["users.manage", "users.manage_team"]}>
      <UsersPage />
    </RequireCapability>
  );
}
