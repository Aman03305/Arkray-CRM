"use client";

import { createContext, type ReactNode, useContext } from "react";

import type { Viewer } from "./viewer";

/**
 * The signed-in viewer. `null` means "not loaded yet" (render skeletons, not guesses).
 * Phase 1 supplies the value from `GET /api/v1/auth/me`.
 */
const ViewerContext = createContext<Viewer | null>(null);

export function ViewerProvider({ viewer, children }: { viewer: Viewer | null; children: ReactNode }) {
  return <ViewerContext.Provider value={viewer}>{children}</ViewerContext.Provider>;
}

export function useViewer(): Viewer | null {
  return useContext(ViewerContext);
}
