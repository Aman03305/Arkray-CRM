import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { EditLeadView } from "@/features/workspace/views";
import { isUuid } from "@/lib/workspace";

export const metadata: Metadata = { title: "Edit lead" };

export default async function Page({ params }: PageProps<"/leads/[leadId]/edit">) {
  const { leadId } = await params;
  if (!isUuid(leadId)) notFound();
  return <EditLeadView leadId={leadId.toLowerCase()} />;
}
