import type { Metadata } from "next";

import { NewOpportunityView } from "@/features/workspace/views";
import { isUuid } from "@/lib/workspace";

export const metadata: Metadata = { title: "New opportunity · User workspace" };

export default async function Page({ searchParams }: PageProps<"/admin/users/[userId]/pipeline/new">) {
  const { lead } = await searchParams;
  return <NewOpportunityView leadId={typeof lead === "string" && isUuid(lead) ? lead.toLowerCase() : undefined} />;
}
