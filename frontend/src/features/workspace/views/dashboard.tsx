"use client";

import { WorkspaceDashboard } from "@/features/dashboard/DashboardView";
import { AdminHome } from "@/features/users/AdminHome";

import { InWorkspace } from "./InWorkspace";

// The organisation-wide dashboard is the administrator's home (ADR-0010).
export function DashboardView() {
  return (
    <InWorkspace>
      {(workspace, segment) =>
        workspace.kind === "organization" ? (
          <AdminHome key="all" />
        ) : (
          <WorkspaceDashboard key={segment} workspace={workspace} />
        )
      }
    </InWorkspace>
  );
}
