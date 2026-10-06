import type { Metadata } from "next";

import { AskWorkspaceView } from "@/features/workspace/views/ask";

export const metadata: Metadata = { title: "Ask Arkray" };

export default function Page() {
  return <AskWorkspaceView />;
}
