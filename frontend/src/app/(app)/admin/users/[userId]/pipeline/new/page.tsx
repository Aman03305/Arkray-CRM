import type { Metadata } from "next";

import { NewOpportunityView } from "@/features/workspace/views";

export const metadata: Metadata = { title: "New opportunity · User workspace" };

export default function Page() {
  return <NewOpportunityView />;
}
