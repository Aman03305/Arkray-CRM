import type { Metadata } from "next";

import { NotFoundView } from "@/components/ui/NotFoundView";

export const metadata: Metadata = { title: "Page not found" };

/**
 * A missing page inside the signed-in shell: the shell's own <main> holds it. The root
 * not-found page (signed out, unknown top-level paths) brings its own <main> and full-page
 * height; rendered here, it nested a second main landmark and overflowed the viewport
 * (whole-software audit).
 */
export default function NotFound() {
  return <NotFoundView />;
}
