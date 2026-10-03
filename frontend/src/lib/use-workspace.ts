"use client";

import { usePathname } from "next/navigation";
import { useMemo } from "react";

import { useViewer } from "./viewer-context";
import { type Workspace, workspaceFromPathname } from "./workspace";

/** The workspace for the current page, derived from the URL (see ./workspace.ts). */
export function useWorkspace(): Workspace {
  const pathname = usePathname();
  const viewer = useViewer();
  return useMemo(() => workspaceFromPathname(pathname, viewer), [pathname, viewer]);
}
