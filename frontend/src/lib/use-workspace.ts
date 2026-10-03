"use client";

import { usePathname } from "next/navigation";
import { useMemo } from "react";

import { useViewer } from "./viewer-context";
import { type Workspace, workspaceFromPathname } from "./workspace";

/**
 * The workspace for the current page, derived from the URL (see ./workspace.ts); null when
 * the URL names no workspace (a malformed user id), which views must treat as "not found".
 */
export function useWorkspace(): Workspace | null {
  const pathname = usePathname();
  const viewer = useViewer();
  return useMemo(() => workspaceFromPathname(pathname, viewer), [pathname, viewer]);
}
