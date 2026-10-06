import type { Metadata } from "next";

import { DashboardView } from "@/features/workspace/views/dashboard";

// The same Dashboard view as /dashboard; the workspace (this user) comes from the URL.
export const metadata: Metadata = { title: "Dashboard · User workspace" };

export default function Page() {
  return <DashboardView />;
}
