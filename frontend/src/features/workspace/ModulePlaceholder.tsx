"use client";

import type { LucideIcon } from "lucide-react";

import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { useWorkspace } from "@/lib/use-workspace";
import { describeWorkspace } from "@/lib/workspace";

/**
 * Stand-in for a module view until its phase lands (Leads: 2, Pipeline: 3, Activities: 4,
 * Dashboard figures: 5). An honest empty state: no sample or invented data, in any workspace.
 */
export function ModulePlaceholder({ title, icon }: { title: string; icon: LucideIcon }) {
  const workspace = useWorkspace();
  return (
    <>
      <PageHeader title={title} subtitle={describeWorkspace(workspace)} />
      <EmptyState
        icon={icon}
        title={`${title} isn't available yet`}
        description={
          workspace.kind === "user"
            ? `This user's ${title.toLowerCase()} will appear here once the module is enabled.`
            : `Your ${title.toLowerCase()} will appear here once the module is enabled.`
        }
      />
    </>
  );
}
