import type { Metadata } from "next";

import { NewOpportunityView } from "@/features/workspace/views";
import { isUuid } from "@/lib/workspace";

export const metadata: Metadata = { title: "New opportunity" };

// `?lead={uuid}` (from a lead's page) preselects the lead; anything else is ignored.
export default async function Page({ searchParams }: PageProps<"/pipeline/new">) {
  const { lead } = await searchParams;
  return <NewOpportunityView leadId={typeof lead === "string" && isUuid(lead) ? lead.toLowerCase() : undefined} />;
}
