import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { ActivityView } from "@/features/workspace/views";
import { isUuid } from "@/lib/workspace";

export const metadata: Metadata = { title: "Activity · User workspace" };

export default async function Page({ params }: PageProps<"/admin/users/[userId]/activities/[activityId]">) {
  const { activityId } = await params;
  if (!isUuid(activityId)) notFound();
  return <ActivityView activityId={activityId.toLowerCase()} />;
}
