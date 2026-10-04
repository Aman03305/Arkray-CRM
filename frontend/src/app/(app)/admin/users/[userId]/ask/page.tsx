import type { Metadata } from "next";

import { AskWorkspaceView } from "@/features/workspace/views";

// A static segment: it wins over the [section] route, which serves the four CRM modules.
export const metadata: Metadata = { title: "Ask Arkray · User workspace" };

export default function Page() {
  return <AskWorkspaceView />;
}
