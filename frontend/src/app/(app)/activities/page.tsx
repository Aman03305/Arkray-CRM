import type { Metadata } from "next";

import { ActivitiesView } from "@/features/workspace/views/activities";

export const metadata: Metadata = { title: "Activities" };

export default function Page() {
  return <ActivitiesView />;
}
