"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Plus, SquareKanban } from "lucide-react";
import Link from "next/link";
import { type DragEvent, type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PageHeader } from "@/components/ui/PageHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { OwnerSelect } from "@/features/leads/OwnerSelect";
import { describeError, isApiError } from "@/lib/api/errors";
import type { Board, BoardColumn, OpportunityCard, PipelineTotals, Stage } from "@/lib/api/types";
import { useFlash } from "@/lib/flash";
import { businessToday } from "@/lib/format";
import { formatPercent } from "@/lib/money";
import { useViewer } from "@/lib/viewer-context";
import { describeWorkspace, newOpportunityHref, type Workspace, workspaceApiSegment } from "@/lib/workspace";

import { activeBoardFilterCount, type BoardFilters, CARDS_PER_STAGE, pipelineApi, pipelineKeys } from "./api";
import { pipelinePermissions, useBoardState, usePipelines, useWideLayout } from "./hooks";
import { DRAG_TYPE, OpportunityCardView } from "./OpportunityCardView";
import { Amount, StageName } from "./PipelineBits";
import { StageListView } from "./StageListView";
import { TransitionDialog } from "./TransitionDialog";
import { transitionKind } from "./transitions";
import { moveErrorMessage, useMoveOpportunity } from "./useMoveOpportunity";

type Problem = { message: string; requestId: string | null };

/**
 * The Pipeline, rendered unchanged in every workspace: a salesperson's own (/pipeline),
 * the organisation for administrators (/pipeline), and one user's pipeline opened by an
 * administrator (/admin/users/{id}/pipeline). Only the API path differs, and every query
 * key carries the workspace, so one user's cards or totals never appear under another's.
 */
export function PipelineBoardView({ workspace }: { workspace: Workspace }) {
  const viewer = useViewer();
  const segment = workspaceApiSegment(workspace);
  const permissions = pipelinePermissions(viewer, workspace);
  const wide = useWideLayout();
  const state = useBoardState(segment);
  const pipelines = usePipelines();
  const [notice, setNotice] = useFlash();
  const [problem, setProblem] = useState<Problem | null>(null);
  const [confirm, setConfirm] = useState<{ card: OpportunityCard; target: Stage } | null>(null);
  const [dialogError, setDialogError] = useState<Problem | null>(null);
  const [dragOver, setDragOver] = useState<string | null>(null);
  const focusCard = useRef<{ id: string; stageId: string } | null>(null);
  const noticeRegion = useRef<HTMLDivElement>(null);
  const today = businessToday();

  const filters = state.applied;
  const cardsPerStage = wide ? CARDS_PER_STAGE : 0; // phones list one stage at a time
  const boardKey = [...pipelineKeys.board(workspace, filters), cardsPerStage] as const;
  const board = useQuery({
    queryKey: boardKey,
    queryFn: () => pipelineApi.board(workspace, filters, cardsPerStage),
    // While filters change, keep showing the previous board, but only this workspace's.
    placeholderData: (previous, query) => (query?.queryKey[2] === segment ? previous : undefined),
  });
  const move = useMoveOpportunity(workspace, boardKey);

  // After a move the card re-renders in another column; keyboard users keep their place
  // on it (its "Move" button), instead of focus falling back to the page.
  useEffect(() => {
    const target = focusCard.current;
    if (!target || confirm) return;
    const card = Array.from(document.querySelectorAll<HTMLElement>("[data-card-id]")).find(
      (el) => el.dataset.cardId === target.id && el.dataset.stageId === target.stageId,
    );
    const trigger = card?.querySelector<HTMLElement>('button[aria-haspopup="menu"]');
    if (trigger) {
      trigger.focus();
      focusCard.current = null;
    } else if (!move.isPending && !board.isFetching) {
      // It isn't shown where it went (a stage list, or beyond a column's first cards):
      // stay on the page, on the message saying where it went.
      noticeRegion.current?.focus();
      focusCard.current = null;
    }
  }, [confirm, board.data, board.isFetching, move.isPending]);

  const data = board.data;
  const columns = data?.columns ?? [];
  const stages = columns.map((c) => c.stage);

  function runMove(card: OpportunityCard, target: Stage, lostReason = "", fromDialog = false) {
    setProblem(null);
    setNotice(null);
    setDialogError(null);
    focusCard.current = { id: card.id, stageId: target.id };
    move.mutate(
      { id: card.id, title: card.title, version: card.version, target, lostReason },
      {
        onSuccess: () => {
          setConfirm(null);
          const kind = transitionKind({ stageId: card.stage_id, status: card.status }, target);
          setNotice(
            kind === "win"
              ? `"${card.title}" was marked as won.`
              : kind === "lose"
                ? `"${card.title}" was marked as lost.`
                : kind === "reopen"
                  ? `"${card.title}" was reopened in ${target.name}.`
                  : `"${card.title}" moved to ${target.name}.`,
          );
        },
        onError: (error) => {
          focusCard.current = { id: card.id, stageId: card.stage_id }; // back where it was
          const message = moveErrorMessage(error, { title: card.title, target });
          if (fromDialog && !isApiError(error, 404)) setDialogError(message);
          else {
            setConfirm(null);
            setProblem(message);
          }
        },
      },
    );
  }

  function requestMove(card: OpportunityCard, target: Stage) {
    if (!permissions.canWrite) return;
    if (move.isPending) {
      setProblem({ message: "The previous move is still being saved. Try again in a moment.", requestId: null });
      return;
    }
    const kind = transitionKind({ stageId: card.stage_id, status: card.status }, target);
    if (kind === "same") return;
    if (kind === "reopen-first") {
      setProblem({ message: `"${card.title}" is closed. Reopen it by moving it to an open stage first.`, requestId: null });
      return;
    }
    if (kind === "move") runMove(card, target);
    else {
      setDialogError(null);
      setConfirm({ card, target });
    }
  }

  const findCard = (id: string) => columns.flatMap((c) => c.cards).find((c) => c.id === id);

  if (isApiError(board.error, 404)) return <NotFoundView />;

  const newOpportunity = permissions.canWrite ? (
    <Link
      href={newOpportunityHref(workspace)}
      className="inline-flex h-9 items-center justify-center gap-2 rounded-md bg-brand-600 px-3.5 text-sm font-medium text-white hover:bg-brand-700"
    >
      <Plus aria-hidden="true" className="size-4" />
      New opportunity
    </Link>
  ) : null;
  const activePipelines = (pipelines.data?.results ?? []).filter((p) => p.is_active);
  const total = columns.reduce((sum, c) => sum + c.count, 0);
  const filtered = activeBoardFilterCount(filters) > 0;
  const showOwner = workspace.kind === "organization";
  const listStage = wide ? columns.find((c) => c.stage.id === state.stage) : null;

  return (
    <>
      <PageHeader
        title="Pipeline"
        subtitle={
          <>
            {describeWorkspace(workspace)}
            {data ? ` · ${data.pipeline.name}` : null}
          </>
        }
        actions={newOpportunity}
      />

      <BoardFiltersBar
        workspace={workspace}
        filters={state.filters}
        rangeInvalid={state.rangeInvalid}
        pipelines={activePipelines.map((p) => ({ id: p.id, name: p.name }))}
        defaultPipeline={data?.pipeline.id ?? ""}
        onChange={(patch) => {
          setNotice(null);
          state.setFilters(patch);
        }}
        onClear={state.resetFilters}
      />

      {data ? <TotalsBar totals={data.totals} updating={move.isPending || (board.isFetching && board.isPlaceholderData)} /> : null}

      <div ref={noticeRegion} tabIndex={-1} aria-live="polite" className="mb-3 space-y-2 empty:hidden focus:outline-none">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
      </div>
      {problem ? (
        <div className="mb-3">
          <Alert tone="error" requestId={problem.requestId}>
            {problem.message}
          </Alert>
        </div>
      ) : null}

      {board.isError && !data ? (
        <BoardError error={board.error} retrying={board.isFetching} onRetry={() => void board.refetch()} />
      ) : !data ? (
        <BoardSkeleton />
      ) : (
        <>
          {board.isError ? (
            <div className="mb-3">
              <Alert tone="info">The pipeline couldn&apos;t be refreshed just now; this is what was last loaded.</Alert>
            </div>
          ) : null}
          {total === 0 ? (
            filtered ? (
              <EmptyState
                icon={SquareKanban}
                title="No opportunities match these filters"
                action={
                  <Button variant="secondary" onClick={state.resetFilters}>
                    Clear filters
                  </Button>
                }
              />
            ) : (
              <EmptyState
                icon={SquareKanban}
                title="No opportunities yet"
                description={
                  workspace.kind === "user"
                    ? "This user has no opportunities in this pipeline."
                    : "Create an opportunity for a lead, or convert a qualified lead from its page."
                }
                action={newOpportunity}
              />
            )
          ) : wide && listStage ? (
            <section aria-labelledby="stage-list-heading">
              <div className="mb-3 flex flex-wrap items-center gap-3">
                <Button variant="secondary" size="sm" icon={<ArrowLeft aria-hidden="true" className="size-4" />} onClick={() => state.setStage(null)}>
                  Back to the board
                </Button>
                <h2 id="stage-list-heading" className="text-base font-semibold text-slate-900">
                  <StageName stage={listStage.stage} />{" "}
                  <span className="font-normal text-slate-500">· {listStage.count} opportunities</span>
                </h2>
              </div>
              <div className="max-w-3xl">
                <StageListView
                  key={`${listStage.stage.id}|${JSON.stringify(filters)}`}
                  workspace={workspace}
                  filters={filters}
                  column={listStage}
                  stages={stages}
                  showOwner={showOwner}
                  canWrite={permissions.canWrite}
                  movingId={move.isPending ? (move.variables?.id ?? null) : null}
                  onMove={requestMove}
                  today={today}
                />
              </div>
            </section>
          ) : wide ? (
            // `relative`: screen-reader-only text inside the columns is absolutely positioned;
            // without a positioned scroll container it escapes it and widens the whole page
            // (live walkthrough at 1440 px).
            <div role="list" aria-label="Stages" className="relative -mx-1 flex gap-3 overflow-x-auto px-1 pb-4">
              {columns.map((column) => (
                <BoardColumnView
                  key={column.stage.id}
                  column={column}
                  workspace={workspace}
                  stages={stages}
                  showOwner={showOwner}
                  canWrite={permissions.canWrite}
                  movingId={move.isPending ? (move.variables?.id ?? null) : null}
                  highlighted={dragOver === column.stage.id}
                  onDragOverStage={setDragOver}
                  onDropCard={(id) => {
                    setDragOver(null);
                    const card = findCard(id);
                    if (card) requestMove(card, column.stage);
                  }}
                  onMove={requestMove}
                  onViewAll={() => state.setStage(column.stage.id)}
                  today={today}
                />
              ))}
            </div>
          ) : (
            <StageTabs
              board={data}
              selected={state.stage}
              onSelect={state.setStage}
              render={(column) => (
                <StageListView
                  key={`${column.stage.id}|${JSON.stringify(filters)}`}
                  workspace={workspace}
                  filters={filters}
                  column={column}
                  stages={stages}
                  showOwner={showOwner}
                  canWrite={permissions.canWrite}
                  movingId={move.isPending ? (move.variables?.id ?? null) : null}
                  onMove={requestMove}
                  today={today}
                />
              )}
            />
          )}
        </>
      )}

      {confirm ? (
        <TransitionDialog
          subject={{ title: confirm.card.title, stageId: confirm.card.stage_id, status: confirm.card.status }}
          stages={stages}
          target={confirm.target}
          busy={move.isPending}
          error={dialogError}
          onConfirm={(target, reason) => runMove(findCard(confirm.card.id) ?? confirm.card, target, reason, true)}
          onClose={() => {
            setConfirm(null);
            setDialogError(null);
          }}
        />
      ) : null}
    </>
  );
}

// --- pieces ----------------------------------------------------------------------------------
function TotalsBar({ totals, updating }: { totals: PipelineTotals; updating: boolean }) {
  return (
    <section aria-label="Pipeline totals" aria-busy={updating || undefined} className="mb-4">
      <dl className="grid gap-3 sm:grid-cols-3">
        {[
          { label: "Pipeline value", value: <Amount value={totals.pipeline_value} /> },
          { label: "Weighted pipeline", value: <Amount value={totals.weighted_pipeline} /> },
          { label: "Open opportunities", value: totals.open_count.toLocaleString("en-IN") },
        ].map((tile) => (
          <div key={tile.label} className="rounded-lg border border-slate-200 bg-white px-4 py-3">
            <dt className="text-xs font-medium text-slate-500">{tile.label}</dt>
            <dd className="mt-1 text-lg font-semibold text-slate-900">{tile.value}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-1.5 text-xs text-slate-500">
        Open opportunities only; won, lost and archived ones don&apos;t count. Weighted pipeline = value × probability.
        {updating ? <span className="ml-1">Updating…</span> : null}
      </p>
    </section>
  );
}

interface ColumnProps {
  column: BoardColumn;
  workspace: Workspace;
  stages: readonly Stage[];
  showOwner: boolean;
  canWrite: boolean;
  movingId: string | null;
  highlighted: boolean;
  onDragOverStage: (stageId: string | null) => void;
  onDropCard: (opportunityId: string) => void;
  onMove: (card: OpportunityCard, target: Stage) => void;
  onViewAll: () => void;
  today: string;
}

function BoardColumnView({
  column,
  workspace,
  stages,
  showOwner,
  canWrite,
  movingId,
  highlighted,
  onDragOverStage,
  onDropCard,
  onMove,
  onViewAll,
  today,
}: ColumnProps) {
  const headingId = useId();
  const accepts = canWrite && column.stage.is_active;
  const onDragOver = (event: DragEvent<HTMLElement>) => {
    if (!accepts || !event.dataTransfer.types.includes(DRAG_TYPE)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    onDragOverStage(column.stage.id);
  };
  const onDrop = (event: DragEvent<HTMLElement>) => {
    if (!accepts) return;
    event.preventDefault();
    const id = event.dataTransfer.getData(DRAG_TYPE);
    if (id) onDropCard(id);
  };
  const hidden = column.count - column.cards.length;
  return (
    <div
      role="listitem"
      aria-labelledby={headingId}
      onDragOver={onDragOver}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) onDragOverStage(null);
      }}
      onDrop={onDrop}
      className={`flex min-w-[15.5rem] flex-1 basis-0 flex-col rounded-lg border p-2 transition-colors ${
        highlighted ? "border-brand-500 bg-brand-50" : "border-transparent bg-slate-100"
      }`}
    >
      <header className="mb-2 px-1">
        <div className="flex items-baseline justify-between gap-2">
          <h2 id={headingId} className="text-sm font-semibold text-slate-900">
            <StageName stage={column.stage} />
          </h2>
          <span className="rounded-full bg-white px-2 text-xs font-medium text-slate-600">
            {column.count}
            <span className="sr-only"> opportunities</span>
          </span>
        </div>
        <p className="mt-0.5 text-xs text-slate-500">
          <Amount value={column.total_value} />
          {column.stage.category === "open" ? (
            <>
              {" · "}
              {formatPercent(column.stage.probability)} · weighted <Amount value={column.weighted_value} />
            </>
          ) : null}
        </p>
      </header>
      {column.cards.length ? (
        <ul className="space-y-2" aria-label={`${column.stage.name} opportunities`}>
          {column.cards.map((card) => (
            <li key={card.id}>
              <OpportunityCardView
                card={card}
                workspace={workspace}
                stages={stages}
                showOwner={showOwner}
                canWrite={canWrite}
                moving={movingId === card.id}
                onMove={onMove}
                today={today}
              />
            </li>
          ))}
        </ul>
      ) : (
        <p className="rounded-md border border-dashed border-slate-300 px-3 py-6 text-center text-xs text-slate-500">
          {accepts ? "Drop an opportunity here, or use its Move menu." : "No opportunities"}
        </p>
      )}
      {hidden > 0 ? (
        <button type="button" onClick={onViewAll} className="mt-2 rounded-md px-2 py-1.5 text-left text-xs font-medium text-brand-700 hover:bg-white">
          View all {column.count} in {column.stage.name}
        </button>
      ) : null}
    </div>
  );
}

/** Phones and narrow tablets: one stage at a time, chosen from tabs (arrow keys move). */
function StageTabs({
  board,
  selected,
  onSelect,
  render,
}: {
  board: Board;
  selected: string | null;
  onSelect: (stageId: string) => void;
  render: (column: BoardColumn) => ReactNode;
}) {
  const baseId = useId();
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  // Without an explicit choice, the first stage holding opportunities, fixed when the tabs
  // first appear: a move that empties it must not silently switch the tab (review).
  const [fallback] = useState(() => (board.columns.find((c) => c.count > 0) ?? board.columns[0])?.stage.id ?? null);
  const current =
    board.columns.find((c) => c.stage.id === (selected ?? fallback)) ?? board.columns[0];
  if (!current) return null;
  const index = board.columns.indexOf(current);

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const last = board.columns.length - 1;
    const next =
      event.key === "ArrowRight" ? (index === last ? 0 : index + 1) : event.key === "ArrowLeft" ? (index === 0 ? last : index - 1) : event.key === "Home" ? 0 : event.key === "End" ? last : null;
    if (next === null) return;
    event.preventDefault();
    onSelect(board.columns[next]!.stage.id);
    tabs.current[next]?.focus();
  };

  return (
    <div>
      <div role="tablist" aria-label="Stages" className="relative -mx-4 mb-3 flex gap-1 overflow-x-auto px-4 pb-1">
        {board.columns.map((column, i) => {
          const active = column === current;
          return (
            <button
              key={column.stage.id}
              ref={(el) => {
                tabs.current[i] = el;
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-${i}`}
              aria-selected={active}
              aria-controls={`${baseId}-panel`}
              tabIndex={active ? 0 : -1}
              onClick={() => onSelect(column.stage.id)}
              onKeyDown={onKeyDown}
              className={`shrink-0 rounded-full border px-3 py-1.5 text-sm ${
                active ? "border-slate-900 bg-slate-900 font-medium text-white" : "border-slate-300 bg-white text-slate-700"
              }`}
            >
              <StageName stage={column.stage} /> <span className={active ? "text-slate-300" : "text-slate-500"}>{column.count}</span>
            </button>
          );
        })}
      </div>
      <div role="tabpanel" id={`${baseId}-panel`} aria-labelledby={`${baseId}-tab-${index}`}>
        <p className="mb-2 text-xs text-slate-500">
          <Amount value={current.total_value} />
          {current.stage.category === "open" ? (
            <>
              {" · "}
              weighted <Amount value={current.weighted_value} />
            </>
          ) : null}
        </p>
        {render(current)}
      </div>
    </div>
  );
}

function BoardFiltersBar({
  workspace,
  filters,
  rangeInvalid,
  pipelines,
  defaultPipeline,
  onChange,
  onClear,
}: {
  workspace: Workspace;
  filters: BoardFilters;
  rangeInvalid: boolean;
  pipelines: { id: string; name: string }[];
  defaultPipeline: string;
  onChange: (patch: Partial<BoardFilters>) => void;
  onClear: () => void;
}) {
  const ids = { pipeline: useId(), from: useId(), to: useId(), range: useId() };
  const control = "h-9 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900";
  const count = activeBoardFilterCount(filters);
  return (
    <div role="group" aria-label="Filter the pipeline" className="mb-4 flex flex-wrap items-end gap-3">
      {pipelines.length > 1 ? (
        <div>
          <label htmlFor={ids.pipeline} className="mb-1 block text-xs font-medium text-slate-600">
            Pipeline
          </label>
          <select id={ids.pipeline} className={control} value={filters.pipeline || defaultPipeline} onChange={(e) => onChange({ pipeline: e.target.value })}>
            {pipelines.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      <div>
        <label htmlFor={ids.from} className="mb-1 block text-xs font-medium text-slate-600">
          Expected close from
        </label>
        <input
          id={ids.from}
          type="date"
          min="2000-01-01"
          max={filters.closeTo || "2099-12-31"}
          className={control}
          value={filters.closeFrom}
          onChange={(e) => onChange({ closeFrom: e.target.value })}
        />
      </div>
      <div>
        <label htmlFor={ids.to} className="mb-1 block text-xs font-medium text-slate-600">
          Expected close to
        </label>
        <input
          id={ids.to}
          type="date"
          min={filters.closeFrom || "2000-01-01"}
          max="2099-12-31"
          className={control}
          value={filters.closeTo}
          onChange={(e) => onChange({ closeTo: e.target.value })}
          aria-invalid={rangeInvalid || undefined}
          aria-describedby={rangeInvalid ? ids.range : undefined}
        />
      </div>
      {rangeInvalid ? (
        <p id={ids.range} role="alert" className="basis-full text-xs text-red-600">
          The end date must be on or after the start date. The pipeline still shows the last valid range.
        </p>
      ) : null}
      {workspace.kind === "organization" ? (
        <div className="min-w-56">
          <OwnerSelect
            label="Owner"
            placeholder="Everyone"
            value={filters.owner}
            valueLabel={filters.ownerLabel}
            onChange={(id, label) => onChange({ owner: id, ownerLabel: label })}
          />
        </div>
      ) : null}
      {count ? (
        <Button variant="ghost" size="sm" onClick={onClear}>
          Clear filters ({count})
        </Button>
      ) : null}
    </div>
  );
}

function BoardSkeleton() {
  return (
    <div aria-busy="true" className="flex gap-3 overflow-hidden">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="w-72 shrink-0 space-y-2 rounded-lg bg-slate-100 p-2">
          <Skeleton className="h-5 w-24" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ))}
      <span className="sr-only">Loading the pipeline</span>
    </div>
  );
}

function BoardError({ error, retrying, onRetry }: { error: unknown; retrying: boolean; onRetry: () => void }) {
  const { message, requestId } = describeError(error);
  const forbidden = isApiError(error, 403);
  return (
    <Alert
      tone="error"
      title={forbidden ? "You can't view this pipeline" : "The pipeline couldn't be loaded"}
      requestId={requestId}
      action={
        forbidden ? null : (
          <Button variant="secondary" size="sm" onClick={onRetry} loading={retrying}>
            Try again
          </Button>
        )
      }
    >
      {message}
    </Alert>
  );
}
