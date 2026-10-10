"use client";

import { FileArchive } from "lucide-react";
import Link from "next/link";
import { useId } from "react";

import { PageHeader } from "@/components/ui/PageHeader";
import { DashboardContent } from "@/features/dashboard/DashboardView";
import { DATA_REQUESTS_HREF } from "@/features/privacy/api";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import type { Workspace } from "@/lib/workspace";

import { SecurityActivity } from "./SecurityActivity";

const ORGANIZATION: Workspace = { kind: "organization" };

/** Where an administrator finds the data exports they requested (privacy.manage). */
function PrivacyRequests() {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="mt-6 rounded-lg border border-slate-200 bg-white px-5 py-4">
      <h2 id={headingId} className="mb-1 flex items-center gap-2 text-sm font-semibold text-slate-900">
        <FileArchive aria-hidden="true" className="size-4 text-slate-500" />
        Privacy
      </h2>
      <p className="text-sm text-slate-600">
        <Link href={DATA_REQUESTS_HREF} className="font-medium text-brand-700 hover:underline">
          Data requests
        </Link>
        : the data exports you requested, to download when they&apos;re ready.
      </p>
    </section>
  );
}

/**
 * The administrator's home: the organisation-wide dashboard (ADR-0010). The same
 * figures and lists as anyone's dashboard, over every user's records (each upcoming meeting
 * and open task names whose it is), then the way to their data requests for those who handle
 * privacy requests, and recent security activity for those who may read the security log.
 * There is no separate admin analytics page: /admin leads here.
 */
export function AdminHome() {
  const viewer = useViewer();
  const audits = hasCapability(viewer, "audit.view");
  const privacy = hasCapability(viewer, "privacy.manage");
  return (
    <>
      <DashboardContent workspace={ORGANIZATION} header={<PageHeader title="Dashboard" subtitle="Organization overview" />} />
      {privacy ? <PrivacyRequests /> : null}
      {audits ? <SecurityActivity /> : null}
    </>
  );
}
