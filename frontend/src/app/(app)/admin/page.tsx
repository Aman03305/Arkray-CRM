import type { Metadata } from "next";

import { AdminEntry } from "@/features/users/AdminEntry";

export const metadata: Metadata = { title: "Admin" };

/* /admin: the administrator's home is the organisation dashboard (ADR-0010). */
export default function AdminPage() {
  return <AdminEntry />;
}
