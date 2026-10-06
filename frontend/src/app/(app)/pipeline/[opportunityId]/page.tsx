import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { OpportunityView } from "@/features/workspace/views/pipeline";
import { isUuid } from "@/lib/workspace";

export const metadata: Metadata = { title: "Opportunity" };

export default async function Page({ params }: PageProps<"/pipeline/[opportunityId]">) {
  const { opportunityId } = await params;
  if (!isUuid(opportunityId)) notFound();
  return <OpportunityView opportunityId={opportunityId.toLowerCase()} />;
}
