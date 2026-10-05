"use client";

import Link from "next/link";
import type { DragEvent } from "react";

import { ActionMenu } from "@/components/ui/ActionMenu";
import type { OpportunityCard, Stage } from "@/lib/api/types";
import { formatPercent } from "@/lib/money";
import { opportunityHref, type Workspace } from "@/lib/workspace";

import { Amount, CloseDate, CustomerName } from "./PipelineBits";
import { moveLabel, moveTargets, transitionKind } from "./transitions";

export const DRAG_TYPE = "application/x-arkray-opportunity";

interface CardProps {
  card: OpportunityCard;
  workspace: Workspace;
  stages: readonly Stage[];
  showOwner: boolean;
  canWrite: boolean;
  /** A move of this card is in flight. */
  moving?: boolean;
  onMove: (card: OpportunityCard, target: Stage) => void;
  today: string;
}

/**
 * One opportunity on the board or a stage list. Drag and drop moves it on wide screens;
 * the "Move" menu does the same for keyboard, screen-reader and touch users (both call the
 * same move operation). The title opens the opportunity.
 */
export function OpportunityCardView({ card, workspace, stages, showOwner, canWrite, moving = false, onMove, today }: CardProps) {
  const from = { stageId: card.stage_id, status: card.status };
  const actions = canWrite
    ? moveTargets(stages, from).map((target) => ({
        key: target.id,
        label: moveLabel(transitionKind(from, target), target),
        onSelect: () => onMove(card, target),
        tone: target.category === "lost" ? ("danger" as const) : ("default" as const),
      }))
    : [];

  const onDragStart = (event: DragEvent<HTMLElement>) => {
    event.dataTransfer.setData(DRAG_TYPE, card.id);
    event.dataTransfer.effectAllowed = "move";
  };

  return (
    <article
      data-card-id={card.id}
      data-stage-id={card.stage_id}
      aria-busy={moving || undefined}
      draggable={canWrite && !moving}
      onDragStart={canWrite ? onDragStart : undefined}
      className={`group rounded-md border border-card-border bg-white px-3 py-2.5 shadow-[0_1px_1px_rgba(15,40,60,0.04)] transition-shadow ${
        moving ? "opacity-60" : "hover:border-slate-300 hover:shadow-md"
      } ${canWrite ? "cursor-grab active:cursor-grabbing" : ""}`}
    >
      <div className="flex items-start gap-1">
        <div className="min-w-0 flex-1">
          <h3 className="text-[13px] font-semibold leading-snug text-slate-900">
            <Link href={opportunityHref(workspace, card.id)} title={card.title} className="line-clamp-2 wrap-break-word hover:text-brand-700 hover:underline">
              {card.title}
            </Link>
          </h3>
          <p className="mt-0.5 truncate text-xs text-slate-600" title={card.account_name}>
            {card.account_name || <CustomerName lead={card.lead} />}
          </p>
        </div>
        {actions.length ? (
          <div data-move-trigger-wrapper className="-mr-1.5 -mt-1">
            <ActionMenu label={`Move ${card.title}`} actions={actions} />
          </div>
        ) : null}
      </div>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 border-t border-slate-100 pt-2 text-xs">
        <dt className="sr-only">Value</dt>
        <dd className="col-span-2 text-[13px] font-semibold text-slate-900">
          <Amount value={card.value} />
          <span className="ml-1.5 text-xs font-normal text-slate-500">
            <span className="sr-only">Probability </span>
            {formatPercent(card.probability)}
            {card.probability_overridden ? <span className="sr-only"> (set manually)</span> : null}
          </span>
        </dd>
        {card.negotiated_price && card.status === "open" ? (
          <>
            <dt className="text-slate-500">Agreed</dt>
            <dd className="text-slate-700">
              <span className="sr-only">Negotiated price </span>
              <Amount value={card.negotiated_price} />
            </dd>
          </>
        ) : null}
        <dt className="text-slate-500">Close</dt>
        <dd className="text-slate-700">
          <CloseDate date={card.expected_close_date} open={card.status === "open"} today={today} />
        </dd>
        {showOwner ? (
          <>
            <dt className="text-slate-500">Owner</dt>
            <dd className="truncate text-slate-700">{card.owner.full_name}</dd>
          </>
        ) : null}
      </dl>
      {moving ? <p className="mt-1 text-xs text-slate-500">Saving…</p> : null}
    </article>
  );
}
