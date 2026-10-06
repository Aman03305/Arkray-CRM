import type { Metadata } from "next";

import { AskWorkspaceView } from "@/features/workspace/views/ask";

// A static segment, like the other modules: it wins over the [section] route.
export const metadata: Metadata = { title: "Ask Arkray · User workspace" };

export default function Page() {
  return <AskWorkspaceView />;
}
