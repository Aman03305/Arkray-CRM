import type { ComponentType } from "react";

import type { WorkspaceSection } from "@/lib/workspace";

import { ActivitiesView, DashboardView, LeadsView, PipelineView } from "./views";

/** Section -> module view. Safe to import from Server Components (holds client references). */
export const SECTION_VIEWS: Record<WorkspaceSection, ComponentType> = {
  dashboard: DashboardView,
  pipeline: PipelineView,
  leads: LeadsView,
  activities: ActivitiesView,
};
