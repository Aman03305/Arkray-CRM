import type { Metadata } from "next";

import { ActivitiesView } from "@/features/workspace/views/activities";

// The same Activities view as /activities; the workspace (this user) comes from the URL.
export const metadata: Metadata = { title: "Activities · User workspace" };

export default function Page() {
  return <ActivitiesView />;
}
