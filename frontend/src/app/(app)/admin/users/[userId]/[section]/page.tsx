import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { SECTION_VIEWS } from "@/features/workspace/section-views";
import { WORKSPACE_SECTIONS, type WorkspaceSection } from "@/lib/workspace";

function isSection(value: string): value is WorkspaceSection {
  return (WORKSPACE_SECTIONS as readonly string[]).includes(value);
}

const TITLES: Record<WorkspaceSection, string> = {
  dashboard: "Dashboard",
  pipeline: "Pipeline",
  leads: "Leads",
  activities: "Activities",
};

export async function generateMetadata({ params }: PageProps<"/admin/users/[userId]/[section]">): Promise<Metadata> {
  const { section } = await params;
  // Distinct titles, so screen-reader route announcements tell the pages apart.
  return { title: isSection(section) ? `${TITLES[section]} · User workspace` : "User workspace" };
}

export default async function UserWorkspaceSection({ params }: PageProps<"/admin/users/[userId]/[section]">) {
  const { section } = await params;
  if (!isSection(section)) notFound();
  const View = SECTION_VIEWS[section];
  return <View />;
}
