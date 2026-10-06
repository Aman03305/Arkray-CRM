"use client";

import { ActivitiesListView } from "@/features/activities/ActivitiesListView";
import { ActivityDetailView } from "@/features/activities/ActivityDetailView";

import { InWorkspace } from "./InWorkspace";

export function ActivitiesView() {
  return <InWorkspace>{(workspace, segment) => <ActivitiesListView key={segment} workspace={workspace} />}</InWorkspace>;
}

export function ActivityView({ activityId }: { activityId: string }) {
  return (
    <InWorkspace>
      {(workspace, segment) => (
        <ActivityDetailView key={`${segment}/${activityId}`} workspace={workspace} activityId={activityId} />
      )}
    </InWorkspace>
  );
}
