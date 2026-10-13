"use client";

import { WorkspaceDashboard } from "@/features/dashboard/DashboardView";
import { AdminHome } from "@/features/users/AdminHome";
import { isOverview } from "@/lib/workspace";

import { InWorkspace } from "./InWorkspace";

// The overview dashboard (the organisation's, or a team administrator's team's) is the
// administrator's home (ADR-0010, ADR-0035).
export function DashboardView() {
  return (
    <InWorkspace>
      {(workspace, segment) =>
        isOverview(workspace) ? (
          <AdminHome key="all" workspace={workspace} />
        ) : (
          <WorkspaceDashboard key={segment} workspace={workspace} />
        )
      }
    </InWorkspace>
  );
}
