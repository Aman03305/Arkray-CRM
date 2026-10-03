import type { Metadata } from "next";

import { NewLeadView } from "@/features/workspace/views";

export const metadata: Metadata = { title: "New lead" };

export default function Page() {
  return <NewLeadView />;
}
