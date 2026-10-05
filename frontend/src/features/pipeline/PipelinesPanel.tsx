"use client";

import { PanelLeftClose, PanelLeftOpen, SquareKanban } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useId } from "react";

import { setPanelCollapsed, usePanelCollapsed } from "@/components/shell/panel-state";
import { Skeleton } from "@/components/ui/Skeleton";
import type { StageCategory } from "@/lib/api/types";
import { type Workspace, workspaceApiSegment, workspaceHref } from "@/lib/workspace";

import { choosePipeline, defaultPipeline, useBoardSelection, usePipelines } from "./hooks";

const STAGE_DOT: Record<StageCategory, string> = {
  open: "bg-stage-open",
  won: "bg-stage-won",
  lost: "bg-stage-lost",
};

const PANEL_LINK = "focus-on-shell flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors focus-visible:-outline-offset-2";
const PANEL_LINK_IDLE = "text-shell-muted hover:bg-white/10 hover:text-white";
const PANEL_LINK_SELECTED = "bg-shell-active font-semibold text-white";

const TOGGLE = "focus-on-shell rounded-md p-1.5 text-shell-muted hover:bg-white/10 hover:text-white";

/**
 * Bigin's secondary panel on Pipeline pages (desktop): the pipelines, and the selected
 * pipeline's stages. A pipeline opens its board; a stage opens that stage's full list (as
 * the board's "View all" does). It can be collapsed to a thin strip, remembered in this
 * browser.
 */
export function PipelinesPanel({ workspace }: { workspace: Workspace }) {
  const pathname = usePathname();
  const collapsed = usePanelCollapsed();
  const pipelines = usePipelines(workspace);
  const key = workspaceApiSegment(workspace);
  const selection = useBoardSelection(key);
  const headingId = useId();
  const boardHref = workspaceHref(workspace, "pipeline");
  const onBoard = pathname === boardHref;

  if (collapsed) {
    return (
      <div className="flex h-full flex-col items-center bg-shell-panel pt-3">
        <button type="button" onClick={() => setPanelCollapsed(false)} aria-label="Show pipelines" title="Show pipelines" className={TOGGLE}>
          <PanelLeftOpen aria-hidden="true" className="size-4" />
        </button>
      </div>
    );
  }

  const active = (pipelines.data?.results ?? []).filter((p) => p.is_active);
  const current = active.find((p) => p.id === selection.pipeline) ?? defaultPipeline(active);

  return (
    <section aria-labelledby={headingId} className="flex h-full flex-col bg-shell-panel text-white">
      <div className="flex items-center justify-between gap-2 px-4 pb-2 pt-3.5">
        <h2 id={headingId} className="text-sm font-semibold">
          Pipelines
        </h2>
        <button type="button" onClick={() => setPanelCollapsed(true)} aria-label="Hide pipelines" title="Hide pipelines" className={TOGGLE}>
          <PanelLeftClose aria-hidden="true" className="size-4" />
        </button>
      </div>

      <div className="scroll-slim flex-1 overflow-y-auto px-2 pb-4">
        {pipelines.isPending ? (
          <div aria-busy="true" className="space-y-2 px-2.5 py-2">
            <Skeleton className="h-4 w-36 bg-white/20!" />
            <Skeleton className="h-4 w-28 bg-white/20!" />
            <span className="sr-only">Loading pipelines</span>
          </div>
        ) : pipelines.isError ? (
          <p className="px-2.5 py-2 text-xs text-shell-muted">The pipelines couldn&apos;t be loaded.</p>
        ) : (
          <ul className="space-y-0.5">
            {active.map((pipeline) => {
              const selected = pipeline.id === current?.id;
              return (
                <li key={pipeline.id}>
                  <Link
                    href={boardHref}
                    onNavigate={() => choosePipeline(key, pipeline.id)}
                    aria-current={selected && onBoard && selection.stage === null ? "page" : undefined}
                    className={`${PANEL_LINK} ${selected ? PANEL_LINK_SELECTED : PANEL_LINK_IDLE}`}
                  >
                    <span aria-hidden="true" className="flex size-6 shrink-0 items-center justify-center rounded border border-white/25">
                      <SquareKanban className="size-3.5" />
                    </span>
                    <span className="min-w-0 truncate">{pipeline.name}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}

        {current ? (
          <>
            <h3 className="px-2.5 pb-1.5 pt-5 text-xs font-semibold uppercase tracking-wide text-shell-muted">Stages</h3>
            <ul className="space-y-0.5">
              {current.stages
                .filter((stage) => stage.is_active)
                .map((stage) => {
                  const listed = onBoard && selection.stage === stage.id;
                  return (
                    <li key={stage.id}>
                      <Link
                        href={boardHref}
                        onNavigate={() => choosePipeline(key, current.id, stage.id)}
                        aria-current={listed ? "page" : undefined}
                        title={`All opportunities in ${stage.name}`}
                        className={`${PANEL_LINK} py-1.5 ${listed ? PANEL_LINK_SELECTED : PANEL_LINK_IDLE}`}
                      >
                        <span aria-hidden="true" className={`size-2 shrink-0 rounded-full ${STAGE_DOT[stage.category]}`} />
                        <span className="min-w-0 truncate">{stage.name}</span>
                      </Link>
                    </li>
                  );
                })}
            </ul>
          </>
        ) : null}
      </div>
    </section>
  );
}
