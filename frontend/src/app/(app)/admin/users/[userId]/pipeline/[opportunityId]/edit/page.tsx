import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { EditOpportunityView } from "@/features/workspace/views/pipeline";
import { isUuid } from "@/lib/workspace";

export const metadata: Metadata = { title: "Edit opportunity · User workspace" };

export default async function Page({ params }: PageProps<"/admin/users/[userId]/pipeline/[opportunityId]/edit">) {
  const { opportunityId } = await params;
  if (!isUuid(opportunityId)) notFound();
  return <EditOpportunityView opportunityId={opportunityId.toLowerCase()} />;
}
