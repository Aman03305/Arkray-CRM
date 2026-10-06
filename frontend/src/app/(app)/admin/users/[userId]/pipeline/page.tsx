import type { Metadata } from "next";

import { PipelineView } from "@/features/workspace/views/pipeline";

// The same Pipeline view as /pipeline; the workspace (this user) comes from the URL.
export const metadata: Metadata = { title: "Pipeline · User workspace" };

export default function Page() {
  return <PipelineView />;
}
