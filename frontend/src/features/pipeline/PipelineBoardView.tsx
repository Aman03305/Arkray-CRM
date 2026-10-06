"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ListFilter, Plus, Settings2, SquareKanban } from "lucide-react";
import { useRouter } from "next/navigation";
import { type DragEvent, type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { Skeleton } from "@/components/ui/Skeleton";
import { OwnerSelect } from "@/features/users/OwnerSelect";
import { describeError, isApiError } from "@/lib/api/errors";
import type { Board, BoardColumn, OpportunityCard, PipelineTotals, Stage } from "@/lib/api/types";
import { setFlash, useFlash } from "@/lib/flash";
import { businessToday } from "@/lib/format";
import { formatPercent } from "@/lib/money";
import { receivesNewWork, selectedUserId, useWorkspaceSubject } from "@/features/workspace/api";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { opportunityHref, type Workspace, workspaceApiSegment, workspaceHref } from "@/lib/workspace";

import type { AgreedTerms } from "./AgreedTerms";
import { activeBoardFilterCount, type BoardFilters, CARDS_PER_STAGE, pipelineApi, pipelineKeys } from "./api";
import { choosePipeline, isNegotiation, pipelinePermissions, useBoardState, usePipelines, useWideLayout } from "./hooks";
import { DRAG_TYPE, OpportunityCardView } from "./OpportunityCardView";
import { OpportunityDrawer } from "./OpportunityDrawer";
import { PipelineSettings } from "./PipelineSettings";
import { Amount, StageName } from "./PipelineBits";
import { StageListView } from "./StageListView";
import { TransitionDialog } from "./TransitionDialog";
import { transitionKind } from "./transitions";
import { moveErrorMessage, type MoveProblem, useMoveOpportunity } from "./useMoveOpportunity";

type Problem = MoveProblem;

/**
 * The Pipeline, rendered unchanged in every workspace: a salesperson's own (/pipeline),
 * the organisation for administrators (/pipeline), and one user's pipeline opened by an
 * administrator (/admin/users/{id}/pipeline). Only the API path differs, and every query
 * key carries the workspace, so one user's cards or totals never appear under another's.
 */
export function PipelineBoardView({ workspace, create = false }: { workspace: Workspace; create?: boolean }) {
  const viewer = useViewer();
  const router = useRouter();
  // New opportunity: a panel over the board (also opened by the /pipeline/new route).
  const [creating, setCreating] = useState(create);
  const [settings, setSettings] = useState<"edit" | "new" | null>(null);
  const segment = workspaceApiSegment(workspace);
  const permissions = pipelinePermissions(viewer, workspace);
  // A deactivated user takes no new work: no "New opportunity" that could only fail.
  const subject = useWorkspaceSubject(selectedUserId(workspace));
  const wide = useWideLayout();
  const state = useBoardState(segment);
  const pipelines = usePipelines(workspace);
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

  function runMove(card: OpportunityCard, target: Stage, lostReason = "", fromDialog = false, terms?: AgreedTerms) {
    setProblem(null);
    setNotice(null);
    setDialogError(null);
    focusCard.current = { id: card.id, stageId: target.id };
    move.mutate(
      { id: card.id, title: card.title, version: card.version, target, lostReason, terms },
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
    // A negotiation stage asks for the price first: the card stays where it is until then.
    if (kind === "move" && !isNegotiation(target)) runMove(card, target);
    else {
      setDialogError(null);
      setConfirm({ card, target });
    }
  }

  const findCard = (id: string) => columns.flatMap((c) => c.cards).find((c) => c.id === id);

  if (isApiError(board.error, 404)) return <NotFoundView />;

  const newOpportunity = permissions.canWrite && receivesNewWork(subject.data) ? (
    <Button icon={<Plus aria-hidden="true" className="size-4" />} onClick={() => setCreating(true)}>
      New opportunity
    </Button>
  ) : null;
  const activePipelines = (pipelines.data?.results ?? []).filter((p) => p.is_active);
  const current = activePipelines.find((p) => p.id === (data?.pipeline.id ?? filters.pipeline));
  // A pipeline of one's own (or, organisation-wide, a shared one) can be created here.
  const canCreatePipeline =
    workspace.kind === "organization" ? hasCapability(viewer, "config.manage") : permissions.canWrite;
  // Opened by a route (the header's "New opportunity"): closing returns to the board; saving
  // lands on the new deal, with its notice.
  const closeCreate = () => {
    setCreating(false);
    if (create) router.replace(workspaceHref(workspace, "pipeline"));
  };
  const total = columns.reduce((sum, c) => sum + c.count, 0);
  const filtered = activeBoardFilterCount(filters) > 0;
  const showOwner = workspace.kind === "organization";
  const listStage = wide ? columns.find((c) => c.stage.id === state.stage) : null;

  return (
    <>
      {/* Bigin's toolbar: the pipeline (choose, configure), its totals, the "add" button. */}
      <div className="mb-3 rounded-lg border border-card-border bg-white px-3 py-2.5 shadow-xs sm:px-4">
        <h1 className="sr-only">Pipeline</h1>
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2.5">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <PipelinePicker
              pipelines={activePipelines.map((p) => ({ id: p.id, name: p.name, owner: p.owner?.full_name ?? null }))}
              value={data?.pipeline.id ?? filters.pipeline}
              onChange={(pipeline) => {
                setNotice(null);
                choosePipeline(segment, pipeline); // one update: the pipeline and its board view
              }}
            />
            {current?.can_manage ? (
              <Button
                variant="ghost"
                size="sm"
                icon={<Settings2 aria-hidden="true" className="size-4" />}
                onClick={() => setSettings("edit")}
                aria-label="Pipeline settings"
              >
                Settings
              </Button>
            ) : null}
            {canCreatePipeline ? (
              <Button variant="ghost" size="sm" icon={<Plus aria-hidden="true" className="size-4" />} onClick={() => setSettings("new")} aria-label="New pipeline">
                Pipeline
              </Button>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            {data ? <TotalsBar totals={data.totals} updating={move.isPending || (board.isFetching && board.isPlaceholderData)} /> : null}
            {newOpportunity}
          </div>
        </div>

        <div className="mt-2.5 border-t border-slate-100 pt-2.5">
          <BoardFiltersBar
            workspace={workspace}
            filters={state.filters}
            rangeInvalid={state.rangeInvalid}
            onChange={(patch) => {
              setNotice(null);
              state.setFilters(patch);
            }}
            onClear={state.resetFilters}
          />
        </div>
      </div>

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
              <EmptyState icon={SquareKanban} title="No opportunities yet" action={newOpportunity} />
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
            <div role="list" aria-label="Stages" className="scroll-slim relative flex gap-2 overflow-x-auto rounded-lg bg-board p-2">
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

      {creating && permissions.canWrite ? (
        <OpportunityDrawer
          workspace={workspace}
          opportunity={null}
          pipelineId={data?.pipeline.id}
          onClose={closeCreate}
          onSaved={(saved) => {
            if (create) {
              const href = opportunityHref(workspace, saved.id);
              setFlash(`“${saved.title}” created.`, href);
              router.replace(href);
              return;
            }
            setCreating(false);
            state.setFilters({ pipeline: saved.pipeline.id });
            setNotice(`“${saved.title}” created.`);
            focusCard.current = { id: saved.id, stageId: saved.stage.id };
          }}
        />
      ) : null}
      {settings ? (
        <PipelineSettings
          workspace={workspace}
          pipeline={settings === "edit" ? (current ?? null) : null}
          onClose={() => setSettings(null)}
          onSaved={(saved, message) => {
            setSettings(null);
            setNotice(message);
            if (saved.is_active) state.setFilters({ pipeline: saved.id });
            else state.setFilters({ pipeline: "" });
          }}
        />
      ) : null}
      {confirm ? (
        <TransitionDialog
          subject={{ title: confirm.card.title, stageId: confirm.card.stage_id, status: confirm.card.status }}
          stages={stages}
          target={confirm.target}
          busy={move.isPending}
          error={dialogError}
          onConfirm={(target, reason, terms) => runMove(findCard(confirm.card.id) ?? confirm.card, target, reason, true, terms)}
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
    <section aria-label="Pipeline totals" aria-busy={updating || undefined} className="min-w-0">
      <dl className="flex flex-wrap gap-x-6 gap-y-1">
        {[
          { label: "Value", title: "Open opportunities' value", value: <Amount value={totals.pipeline_value} /> },
          { label: "Weighted", title: "Value × probability, open opportunities", value: <Amount value={totals.weighted_pipeline} /> },
          { label: "Open", title: "Open opportunities", value: totals.open_count.toLocaleString("en-IN") },
        ].map((tile) => (
          <div key={tile.label} className="min-w-0" title={tile.title}>
            <dt className="text-xs font-medium text-slate-500">
              {tile.label}
              <span className="sr-only"> ({tile.title})</span>
            </dt>
            <dd className="text-sm font-semibold text-slate-900">{tile.value}</dd>
          </div>
        ))}
      </dl>
      {updating ? <span className="sr-only">Updating…</span> : null}
    </section>
  );
}

/** Bigin's coloured line on each column: blue for open stages, green won, red lost. */
const STAGE_ACCENT: Record<Stage["category"], string> = {
  open: "border-t-stage-open",
  won: "border-t-stage-won",
  lost: "border-t-stage-lost",
};

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
      className={`flex w-64 min-w-64 flex-1 flex-col rounded-md transition-colors ${
        highlighted ? "bg-brand-50 ring-2 ring-brand-400" : "bg-column"
      }`}
    >
      <header className={`rounded-md border-t-[3px] bg-white px-3 pb-2 pt-1.5 shadow-[0_1px_2px_rgba(15,40,60,0.08)] ${STAGE_ACCENT[column.stage.category]}`}>
        <div className="flex items-center justify-between gap-2">
          <h2 id={headingId} className="min-w-0 truncate text-sm font-semibold text-slate-900">
            <StageName stage={column.stage} />
          </h2>
          <span className="shrink-0 rounded-full bg-slate-100 px-2 text-xs font-medium text-slate-600">
            {column.count}
            <span className="sr-only"> opportunities</span>
          </span>
        </div>
        <p className="mt-0.5 truncate text-xs text-slate-500">
          <Amount value={column.total_value} />
          {column.stage.category === "open" ? (
            <>
              {" · "}
              {formatPercent(column.stage.probability)} · weighted <Amount value={column.weighted_value} />
            </>
          ) : null}
        </p>
      </header>
      {/* Each column scrolls on its own, so the board fits the screen (as in Bigin);
          `relative` keeps screen-reader-only text inside the scroll box. */}
      <div className="scroll-slim relative flex-1 overflow-y-auto px-1 pb-2 pt-2 lg:max-h-[calc(100dvh-19rem)] lg:min-h-48">
        {column.cards.length ? (
          <ul className="space-y-1.5" aria-label={`${column.stage.name} opportunities`}>
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
          <p className="flex min-h-28 items-center justify-center rounded-md border border-dashed border-slate-300 px-3 py-6 text-center text-xs text-slate-500">
            {accepts ? "Drop here" : "No opportunities"}
          </p>
        )}
        {hidden > 0 ? (
          <button
            type="button"
            onClick={onViewAll}
            className="mt-1.5 w-full rounded-md border border-dashed border-slate-300 px-2 py-2 text-xs font-medium text-brand-700 hover:border-brand-300 hover:bg-white"
          >
            View all {column.count} in {column.stage.name}
          </button>
        ) : null}
      </div>
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
                active ? "border-shell bg-shell font-medium text-white" : "border-slate-300 bg-white text-slate-700"
              }`}
            >
              <StageName stage={column.stage} /> <span className={active ? "text-shell-muted" : "text-slate-500"}>{column.count}</span>
            </button>
          );
        })}
      </div>
      <div role="tabpanel" id={`${baseId}-panel`} aria-labelledby={`${baseId}-tab-${index}`}>
        {/* The cards' h3 headings follow an h2, as on the board's columns. */}
        <h2 className="sr-only">
          <StageName stage={current.stage} />
        </h2>
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

/** The pipeline shown: the workspace's own pipelines, shared ones and any holding its deals. */
function PipelinePicker({
  pipelines,
  value,
  onChange,
}: {
  pipelines: { id: string; name: string; owner: string | null }[];
  value: string;
  onChange: (pipeline: string) => void;
}) {
  const id = useId();
  if (pipelines.length === 0) return null;
  return (
    <div className="min-w-0">
      <label htmlFor={id} className="sr-only">
        Pipeline
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 max-w-[16rem] truncate rounded-md border border-slate-300 bg-white pl-3 pr-8 text-sm font-semibold text-slate-900 hover:border-slate-400 sm:max-w-xs"
      >
        {pipelines.map((p) => (
          <option key={p.id} value={p.id}>
            {p.owner ? `${p.name} · ${p.owner}` : p.name}
          </option>
        ))}
      </select>
    </div>
  );
}

function BoardFiltersBar({
  workspace,
  filters,
  rangeInvalid,
  onChange,
  onClear,
}: {
  workspace: Workspace;
  filters: BoardFilters;
  rangeInvalid: boolean;
  onChange: (patch: Partial<BoardFilters>) => void;
  onClear: () => void;
}) {
  const ids = { group: useId(), from: useId(), to: useId(), range: useId() };
  // Phones fold the filters behind a button, so the board isn't a screen away (desktop
  // always shows them).
  const [open, setOpen] = useState(false);
  const control = "h-9 rounded-full border border-slate-300 bg-white px-3.5 text-sm text-slate-900 hover:border-slate-400";
  const count = activeBoardFilterCount(filters);
  return (
    <>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={ids.group}
        onClick={() => setOpen((value) => !value)}
        className="inline-flex h-8 items-center gap-1.5 rounded-full border border-slate-300 bg-white px-3 text-sm font-medium text-slate-700 hover:bg-slate-50 lg:hidden"
      >
        <ListFilter aria-hidden="true" className="size-4 text-brand-600" />
        Filters{count ? ` (${count})` : ""}
      </button>
      <div
        id={ids.group}
        role="group"
        aria-label="Filter the pipeline"
        className={`${open ? "mt-3 flex" : "hidden"} flex-wrap items-end gap-3 lg:mt-0 lg:flex`}
      >
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
            The end date must be on or after the start date.
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
    </>
  );
}

function BoardSkeleton() {
  return (
    <div aria-busy="true" className="flex gap-2 overflow-hidden rounded-lg bg-board p-2">
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="w-64 shrink-0 space-y-2 rounded-md bg-column">
          <div className="rounded-md border-t-[3px] border-t-stage-open bg-white px-3 py-2">
            <Skeleton className="h-4 w-28" />
            <Skeleton className="mt-1.5 h-3 w-20" />
          </div>
          <div className="space-y-1.5 px-1 pb-2">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
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
