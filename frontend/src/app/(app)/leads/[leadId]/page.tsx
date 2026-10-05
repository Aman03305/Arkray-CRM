import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { LeadView } from "@/features/workspace/views";
import { isUuid } from "@/lib/workspace";

export const metadata: Metadata = { title: "Lead" };

export default async function Page({ params }: PageProps<"/leads/[leadId]">) {
  const { leadId } = await params;
  if (!isUuid(leadId)) notFound();
  return <LeadView leadId={leadId.toLowerCase()} />;
}
