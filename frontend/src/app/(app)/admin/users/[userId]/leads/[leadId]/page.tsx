import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { LeadView } from "@/features/workspace/views/leads";
import { isUuid } from "@/lib/workspace";

export const metadata: Metadata = { title: "Lead · User workspace" };

export default async function Page({ params }: PageProps<"/admin/users/[userId]/leads/[leadId]">) {
  const { leadId } = await params;
  if (!isUuid(leadId)) notFound();
  return <LeadView leadId={leadId.toLowerCase()} />;
}
