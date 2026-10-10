import type { Metadata } from "next";

import { RequireCapability } from "@/features/auth/RequireCapability";
import { DataRequestsPage } from "@/features/privacy/DataRequestsPage";

export const metadata: Metadata = { title: "Data requests" };

/* /admin/data-requests: the data exports an administrator requested (privacy.manage). */
export default function Page() {
  return (
    <RequireCapability capability="privacy.manage">
      <DataRequestsPage />
    </RequireCapability>
  );
}
