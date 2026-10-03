import type { Metadata } from "next";

import { SECTION_VIEWS } from "@/features/workspace/section-views";

export const metadata: Metadata = { title: "Leads" };

export default function Page() {
  const View = SECTION_VIEWS.leads;
  return <View />;
}
