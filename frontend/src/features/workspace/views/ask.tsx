"use client";

import { NotFoundView } from "@/components/ui/NotFoundView";
import { AskView } from "@/features/ask/AskView";
import { canAsk } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";

import { InWorkspace } from "./InWorkspace";

// Ask Arkray: only for viewers who may ask, in a CRM that has it on (the API refuses
// otherwise too). Keyed by workspace like every module view.
export function AskWorkspaceView() {
  const viewer = useViewer();
  return (
    <InWorkspace>
      {(workspace, segment) => (canAsk(viewer) ? <AskView key={segment} workspace={workspace} /> : <NotFoundView />)}
    </InWorkspace>
  );
}
