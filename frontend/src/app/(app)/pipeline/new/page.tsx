import type { Metadata } from "next";

import { NewOpportunityView } from "@/features/workspace/views/pipeline";

export const metadata: Metadata = { title: "New opportunity" };

export default function Page() {
  return <NewOpportunityView />;
}
