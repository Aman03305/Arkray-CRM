"use client";

import { PageHeader } from "@/components/ui/PageHeader";
import { DashboardContent } from "@/features/dashboard/DashboardView";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import type { Workspace } from "@/lib/workspace";

import { SecurityActivity } from "./SecurityActivity";

const ORGANIZATION: Workspace = { kind: "organization" };

/**
 * The administrator's home: the organisation-wide dashboard (ADR-0010). The same
 * figures and lists as anyone's dashboard, over every user's records (each upcoming meeting
 * and open task names whose it is), then recent security activity for those who may read
 * the security log. There is no separate admin analytics page: /admin leads here.
 */
export function AdminHome() {
  const audits = hasCapability(useViewer(), "audit.view");
  return (
    <>
      <DashboardContent workspace={ORGANIZATION} header={<PageHeader title="Dashboard" subtitle="Organization overview" />} />
      {audits ? <SecurityActivity /> : null}
    </>
  );
}
