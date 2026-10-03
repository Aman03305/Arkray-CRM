import type { Metadata } from "next";

import { LeadsView } from "@/features/workspace/views";

// The same Leads view as /leads; the workspace (this user) comes from the URL.
export const metadata: Metadata = { title: "Leads · User workspace" };

export default function Page() {
  return <LeadsView />;
}
