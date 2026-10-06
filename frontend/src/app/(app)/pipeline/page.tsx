import type { Metadata } from "next";

import { PipelineView } from "@/features/workspace/views/pipeline";

export const metadata: Metadata = { title: "Pipeline" };

export default function Page() {
  return <PipelineView />;
}
