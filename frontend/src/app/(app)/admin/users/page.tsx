import type { Metadata } from "next";

import { RequireCapability } from "@/features/auth/RequireCapability";
import { UsersPage } from "@/features/users/UsersPage";

export const metadata: Metadata = { title: "Users" };

export default function Page() {
  return (
    <RequireCapability capability="users.manage">
      <UsersPage />
    </RequireCapability>
  );
}
