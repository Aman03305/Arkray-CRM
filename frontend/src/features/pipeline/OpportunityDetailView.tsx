"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowLeft, ArrowRight, Pencil } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { Skeleton } from "@/components/ui/Skeleton";
import { CurrentWork } from "@/features/activities/CurrentWork";
import { NoteComposer, Timeline } from "@/features/activities/Timeline";
import { cursorOf } from "@/features/leads/api";
import { PersonName } from "@/features/leads/LeadBits";
import { describeError, isApiError } from "@/lib/api/errors";
import type { Stage, StageHistoryEntry } from "@/lib/api/types";
import { useFlash } from "@/lib/flash";
import { businessToday, formatDateTime, formatRelative } from "@/lib/format";
import { formatPercent } from "@/lib/money";
import { useViewer } from "@/lib/viewer-context";
import { leadHref, opportunityHref, sectionBack, type Workspace, workspaceHref } from "@/lib/workspace";

import { pipelineApi, pipelineKeys } from "./api";
import { pipelinePermissions, useOpportunityWriteSync, usePipelines } from "./hooks";
import { Amount, CloseDate, LeadName, OutcomeBadge, StageName } from "./PipelineBits";
import { TransitionDialog } from "./TransitionDialog";
import { moveErrorMessage, useMoveOpportunity } from "./useMoveOpportunity";

type Pending = { target: Stage | null } | null;

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="mb-3 text-sm font-semibold text-slate-900">{title}</h2>
      {children}
    </section>
  );
}

function Fields({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid gap-x-4 gap-y-3 text-sm sm:grid-cols-[11rem_1fr]">
      {items.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-slate-500">{label}</dt>
          <dd className="min-w-0 break-words text-slate-900">{value ?? <span className="text-slate-400">—</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

function When({ iso }: { iso: string | null }) {
  if (!iso) return <span className="text-slate-400">—</span>;
  return (
    <time dateTime={iso}>
      {formatDateTime(iso)} <span className="text-slate-500">({formatRelative(iso)})</span>
    </time>
  );
}

export function OpportunityDetailView({ workspace, opportunityId }: { workspace: Workspace; opportunityId: string }) {
  const viewer = useViewer();
  const queryClient = useQueryClient();
  const permissions = pipelinePermissions(viewer, workspace);
  const pipelines = usePipelines();
  const [notice, setNotice] = useFlash();
  const [pending, setPending] = useState<Pending>(null);
  const [moveError, setMoveError] = useState<{ message: string; requestId: string | null } | null>(null);
  const [lifecycleDialog, setLifecycleDialog] = useState<"archive" | "restore" | null>(null);
  const detail = useQuery({
    queryKey: pipelineKeys.detail(workspace, opportunityId),
    queryFn: () => pipelineApi.get(workspace, opportunityId),
  });
  const move = useMoveOpportunity(workspace);
  const sync = useOpportunityWriteSync(workspace);
  const lifecycle = useMutation({
    mutationFn: (kind: "archive" | "restore") =>
      kind === "archive"
        ? pipelineApi.archive(workspace, opportunityId, detail.data?.version ?? 0)
        : pipelineApi.restore(workspace, opportunityId, detail.data?.version ?? 0),
    onSuccess: sync,
    onError: (error) => {
      if (isApiError(error, 409)) void queryClient.invalidateQueries({ queryKey: pipelineKeys.detail(workspace, opportunityId) });
    },
  });

  const back = (
    <Link href={workspaceHref(workspace, "pipeline")} className="mb-4 inline-flex items-center gap-1 text-sm text-slate-600 hover:text-slate-900">
      <ArrowLeft aria-hidden="true" className="size-4" />
      Pipeline
    </Link>
  );

  if (isApiError(detail.error, 404)) return <NotFoundView back={sectionBack(workspace, "pipeline")} />;
  if (detail.isError && !detail.data) {
    const { message, requestId } = describeError(detail.error);
    return (
      <>
        {back}
        <Alert
          tone="error"
          title={isApiError(detail.error, 403) ? "You can't view this opportunity" : "This opportunity couldn't be loaded"}
          requestId={requestId}
          action={
            isApiError(detail.error, 403) ? null : (
              <Button variant="secondary" size="sm" onClick={() => void detail.refetch()} loading={detail.isFetching}>
                Try again
              </Button>
            )
          }
        >
          {message}
        </Alert>
      </>
    );
  }
  const opportunity = detail.data;
  if (!opportunity) {
    return (
      <div aria-busy="true" className="space-y-4">
        <Skeleton className="h-7 w-72" />
        <Skeleton className="h-4 w-48" />
        <Skeleton className="h-48 w-full" />
        <span className="sr-only">Loading opportunity</span>
      </div>
    );
  }

  const archived = opportunity.archived_at !== null;
  const canChange = permissions.canWrite && !archived;
  // New work on an opportunity belongs to its lead's owner: only while the lead is here.
  const canAddWork = canChange && !opportunity.lead.restricted;
  const open = opportunity.status === "open";
  const stages = pipelines.data?.results.find((p) => p.id === opportunity.pipeline.id)?.stages ?? [opportunity.stage];
  const won = stages.find((s) => s.category === "won" && s.is_active);
  const lost = stages.find((s) => s.category === "lost" && s.is_active);

  const confirmMove = (target: Stage, lostReason: string) => {
    setMoveError(null);
    move.mutate(
      { id: opportunity.id, title: opportunity.title, version: opportunity.version, target, lostReason },
      {
        onSuccess: (updated) => {
          setPending(null);
          setNotice(
            updated.status === "won"
              ? "Marked as won."
              : updated.status === "lost"
                ? "Marked as lost."
                : open
                  ? `Moved to ${updated.stage.name}.`
                  : `Reopened in ${updated.stage.name}.`,
          );
        },
        onError: (error) => {
          setMoveError(moveErrorMessage(error, { title: opportunity.title, target }));
          if (isApiError(error, 409)) void detail.refetch();
        },
      },
    );
  };

  return (
    <>
      {back}
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="break-words text-xl font-semibold tracking-tight text-slate-900">{opportunity.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-slate-600">
            <span className="sr-only">Status:</span>
            <OutcomeBadge status={opportunity.status} />
            <span>
              Stage: <strong className="font-medium text-slate-900"><StageName stage={opportunity.stage} /></strong>
            </span>
            <span>
              Owner: <strong className="font-medium text-slate-900"><PersonName person={opportunity.owner} /></strong>
            </span>
          </div>
        </div>
        {permissions.canWrite ? (
          <div className="flex flex-wrap items-center gap-2">
            {canChange && open && won ? (
              <Button variant="secondary" onClick={() => { setMoveError(null); setPending({ target: won }); }}>
                Mark as won
              </Button>
            ) : null}
            {canChange && open && lost ? (
              <Button variant="secondary" onClick={() => { setMoveError(null); setPending({ target: lost }); }}>
                Mark as lost
              </Button>
            ) : null}
            {canChange ? (
              <Button variant="secondary" icon={<ArrowRight aria-hidden="true" className="size-4" />} onClick={() => { setMoveError(null); setPending({ target: null }); }}>
                {open ? "Move to stage" : "Reopen"}
              </Button>
            ) : null}
            {archived ? (
              <Button variant="secondary" onClick={() => { lifecycle.reset(); setLifecycleDialog("restore"); }}>
                Restore
              </Button>
            ) : (
              <>
                <Button variant="ghost" icon={<Archive aria-hidden="true" className="size-4" />} onClick={() => { lifecycle.reset(); setLifecycleDialog("archive"); }}>
                  Archive
                </Button>
                <Link
                  href={opportunityHref(workspace, opportunity.id, "edit")}
                  className="inline-flex h-9 items-center gap-2 rounded-md bg-brand-600 px-3.5 text-sm font-medium text-white hover:bg-brand-700"
                >
                  <Pencil aria-hidden="true" className="size-4" />
                  Edit
                </Link>
              </>
            )}
          </div>
        ) : null}
      </header>

      <div aria-live="polite" className="mb-4 empty:hidden">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
      </div>
      {archived ? (
        <div className="mb-4">
          <Alert tone="info" title="This opportunity is archived">
            Archived {formatDateTime(opportunity.archived_at)}. It is hidden from the pipeline and its totals
            {permissions.canWrite ? "; restore it to make changes" : ""}.
          </Alert>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Section title="Summary">
            <Fields
              items={[
                ["Value", <Amount key="v" value={opportunity.value} className="font-semibold" />],
                [
                  "Probability",
                  <span key="p">
                    {formatPercent(opportunity.probability)}{" "}
                    <span className="text-slate-500">
                      {opportunity.probability_overridden ? "(set manually)" : open ? "(stage default)" : `(${opportunity.status})`}
                    </span>
                  </span>,
                ],
                ["Weighted value", <Amount key="w" value={opportunity.weighted_value} />],
                ["Expected close", <CloseDate key="c" date={opportunity.expected_close_date} open={open} today={businessToday()} />],
                ["Pipeline", opportunity.pipeline.name],
                ["Closed", opportunity.closed_at ? <When key="cl" iso={opportunity.closed_at} /> : null],
                ...(opportunity.status === "lost" ? ([["Lost reason", opportunity.lost_reason || null]] as [string, ReactNode][]) : []),
              ]}
            />
          </Section>
          <Section title="Description">
            {opportunity.description ? (
              <p className="whitespace-pre-line break-words text-sm text-slate-900">{opportunity.description}</p>
            ) : (
              <p className="text-sm text-slate-400">No description</p>
            )}
          </Section>
          <Timeline
            workspace={workspace}
            subject={{ kind: "opportunity", id: opportunity.id }}
            composer={canAddWork ? <NoteComposer workspace={workspace} link={{ opportunity: opportunity.id }} /> : null}
          />
          <StageHistory workspace={workspace} opportunityId={opportunity.id} version={opportunity.version} />
        </div>
        <div className="space-y-4">
          <Section title="Lead">
            {opportunity.lead.restricted || !opportunity.lead.id ? (
              <p className="text-sm text-slate-500">
                <LeadName lead={opportunity.lead} />. The lead has been reassigned since this opportunity was closed; its
                details are visible in its current owner&apos;s workspace.
              </p>
            ) : (
              <p className="text-sm">
                <Link href={leadHref(workspace, opportunity.lead.id)} className="font-medium text-brand-700 hover:underline">
                  {opportunity.lead.display_name}
                </Link>
                {opportunity.lead.organization_name && opportunity.lead.organization_name !== opportunity.lead.display_name ? (
                  <span className="block text-slate-500">{opportunity.lead.organization_name}</span>
                ) : null}
              </p>
            )}
          </Section>
          {opportunity.lead.restricted ? null : (
            <CurrentWork
              workspace={workspace}
              target={{ opportunity: opportunity.id }}
              label={opportunity.title}
              canWrite={canAddWork}
            />
          )}
          <Section title="Record details">
            <Fields
              items={[
                ["Created by", <PersonName key="cb" person={opportunity.created_by} />],
                ["Created", <When key="ca" iso={opportunity.created_at} />],
                ["Last updated", <When key="ua" iso={opportunity.updated_at} />],
              ]}
            />
          </Section>
        </div>
      </div>

      {pending ? (
        <TransitionDialog
          subject={{ title: opportunity.title, stageId: opportunity.stage.id, status: opportunity.status }}
          stages={stages}
          target={pending.target}
          busy={move.isPending}
          error={moveError}
          onConfirm={confirmMove}
          onClose={() => {
            setPending(null);
            setMoveError(null);
          }}
        />
      ) : null}
      <ConfirmDialog
        open={lifecycleDialog !== null}
        title={lifecycleDialog === "archive" ? `Archive ${opportunity.title}?` : `Restore ${opportunity.title}?`}
        confirmLabel={lifecycleDialog === "archive" ? "Archive opportunity" : "Restore opportunity"}
        tone={lifecycleDialog === "archive" ? "danger" : "primary"}
        busy={lifecycle.isPending}
        error={
          lifecycle.isError
            ? isApiError(lifecycle.error, 409)
              ? { message: "Someone else changed this opportunity a moment ago. The latest details are shown now; please try again.", requestId: null }
              : describeError(lifecycle.error)
            : null
        }
        onConfirm={() => {
          const kind = lifecycleDialog === "archive" ? "archive" : "restore";
          lifecycle.mutate(kind, {
            onSuccess: () => {
              setLifecycleDialog(null);
              setNotice(kind === "archive" ? "Opportunity archived." : "Opportunity restored.");
            },
          });
        }}
        onCancel={() => setLifecycleDialog(null)}
      >
        {lifecycleDialog === "archive"
          ? "It will be hidden from the pipeline and no longer count in its totals. Nothing is deleted, its history is kept, and it can be restored at any time. (Closing it as won or lost is different: that stays on the pipeline.)"
          : "It will appear in the pipeline again and count in its totals if it is open."}
      </ConfirmDialog>
    </>
  );
}

/** Append-only history, newest first, 50 at a time. Read-only by design. */
function StageHistory({ workspace, opportunityId, version }: { workspace: Workspace; opportunityId: string; version: number }) {
  const [cursor, setCursor] = useState<string | null>(null);
  const history = useQuery({
    // The version is part of the key: every change reloads the history.
    queryKey: [...pipelineKeys.history(workspace, opportunityId, cursor), version],
    queryFn: () => pipelineApi.history(workspace, opportunityId, cursor),
    // This opportunity's rows stay on screen while the new version's history loads.
    placeholderData: (previous) => previous,
  });
  const rows = history.data?.results;
  return (
    <Section title="Stage history">
      {history.isError ? (
        <p className="text-sm text-red-700">The history couldn&apos;t be loaded. {describeError(history.error).message}</p>
      ) : !rows ? (
        <Skeleton className="h-16 w-full" />
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-400">No stage changes yet.</p>
      ) : (
        <ol className="space-y-3">
          {rows.map((row) => (
            <HistoryRow key={row.id} row={row} />
          ))}
        </ol>
      )}
      {history.data?.next || history.data?.previous ? (
        <nav aria-label="History pages" className="mt-3 flex justify-end gap-2">
          <Button variant="secondary" size="sm" disabled={!history.data?.previous} onClick={() => setCursor(cursorOf(history.data?.previous))}>
            Newer
          </Button>
          <Button variant="secondary" size="sm" disabled={!history.data?.next} onClick={() => setCursor(cursorOf(history.data?.next))}>
            Older
          </Button>
        </nav>
      ) : null}
    </Section>
  );
}

function HistoryRow({ row }: { row: StageHistoryEntry }) {
  const created = !row.from_stage_name;
  return (
    <li className="border-l-2 border-slate-200 pl-3 text-sm">
      <p className="text-slate-900">
        {created ? (
          <>
            Created in <strong className="font-medium">{row.to_stage_name}</strong>
          </>
        ) : (
          <>
            <strong className="font-medium">{row.from_stage_name}</strong> → <strong className="font-medium">{row.to_stage_name}</strong>
            {row.to_status !== "open" && row.from_status === "open" ? ` (${row.to_status})` : null}
            {row.from_status !== "open" && row.to_status === "open" ? " (reopened)" : null}
          </>
        )}
      </p>
      <p className="text-xs text-slate-500">
        <PersonName person={row.actor} /> · <time dateTime={row.occurred_at}>{formatDateTime(row.occurred_at)}</time> ·{" "}
        <Amount value={row.value} /> at {formatPercent(row.probability)}
      </p>
      {row.lost_reason ? <p className="mt-0.5 whitespace-pre-line text-xs text-slate-600">Reason: {row.lost_reason}</p> : null}
    </li>
  );
}
