"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Pencil } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { ActionMenu } from "@/components/ui/ActionMenu";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { TextField } from "@/components/ui/Field";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PersonName } from "@/components/ui/PersonName";
import { Skeleton } from "@/components/ui/Skeleton";
import { activityKeys, timelineKeys } from "@/features/activities/api";
import { leadKeys } from "@/features/leads/api";
import { CurrentWork } from "@/features/activities/CurrentWork";
import { DealNotes } from "@/features/activities/DealNotes";
import { Timeline } from "@/features/activities/Timeline";
import { OwnerSelect } from "@/features/users/OwnerSelect";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import { cursorOf } from "@/lib/api/pagination";
import type { Opportunity, Stage, StageHistoryEntry } from "@/lib/api/types";
import { mailtoHref, telHref } from "@/lib/contact-links";
import { setFlash, useFlash } from "@/lib/flash";
import { businessToday, formatDateOnly, formatDateTime, formatRelative } from "@/lib/format";
import { formatPercent, parseAmountInput } from "@/lib/money";
import { useViewer } from "@/lib/viewer-context";
import { leadHref, opportunityHref, sectionBack, type Workspace, workspaceApiSegment, workspaceHref } from "@/lib/workspace";

import { pipelineApi, pipelineKeys } from "./api";
import { formatCustomValue } from "./CustomFields";
import { isNegotiation, pipelinePermissions, useOpportunityWriteSync, usePipeline } from "./hooks";
import { OpportunityDrawer } from "./OpportunityDrawer";
import { Amount, CloseDate, OutcomeBadge, StageName } from "./PipelineBits";
import { TransitionDialog } from "./TransitionDialog";
import { moveErrorMessage, useMoveOpportunity } from "./useMoveOpportunity";

type Pending = { target: Stage | null } | null;
type Tab = "overview" | "notes" | "history";
const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "notes", label: "Notes" },
  { id: "history", label: "History" },
];

function Section({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="rounded-lg border border-slate-200 bg-white p-4 sm:p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id={id} className="text-sm font-semibold text-slate-900">
          {title}
        </h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

function Fields({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid gap-x-4 gap-y-2.5 text-sm sm:grid-cols-[10rem_1fr]">
      {items.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-slate-500">{label}</dt>
          <dd className="min-w-0 break-words text-slate-900">{value || value === 0 ? value : <span className="text-slate-400">—</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

function When({ iso }: { iso: string | null }) {
  if (!iso) return <span className="text-slate-400">—</span>;
  return (
    <time dateTime={iso} title={formatDateTime(iso)}>
      {formatRelative(iso)}
    </time>
  );
}

export function OpportunityDetailView({
  workspace,
  opportunityId,
  editOnOpen = false,
}: {
  workspace: Workspace;
  opportunityId: string;
  /** The /edit route: open the edit panel over the deal. */
  editOnOpen?: boolean;
}) {
  const viewer = useViewer();
  const router = useRouter();
  const queryClient = useQueryClient();
  const permissions = pipelinePermissions(viewer, workspace);
  const [notice, setNotice] = useFlash();
  const [pending, setPending] = useState<Pending>(null);
  const [moveError, setMoveError] = useState<{ message: string; requestId: string | null } | null>(null);
  const [lifecycleDialog, setLifecycleDialog] = useState<"archive" | "restore" | null>(null);
  const [editing, setEditing] = useState(editOnOpen);
  const [priceDialog, setPriceDialog] = useState(false);
  const [ownerDialog, setOwnerDialog] = useState(false);
  const [tab, setTab] = useState<Tab>("overview");
  const detail = useQuery({
    queryKey: pipelineKeys.detail(workspace, opportunityId),
    queryFn: () => pipelineApi.get(workspace, opportunityId),
  });
  const pipeline = usePipeline(workspace, detail.data?.pipeline.id);
  // After "Change owner" handed the deal out of this workspace: what this page cached for it
  // here is dropped once the page is gone, so coming back never shows it as it was before
  // the "not found" (as the lead page did after a reassignment, Phase 6 review).
  const leftBehind = useRef<(readonly unknown[])[]>([]);
  useEffect(
    () => () => {
      for (const queryKey of leftBehind.current) queryClient.removeQueries({ queryKey });
    },
    [queryClient],
  );
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
    <Link href={workspaceHref(workspace, "pipeline")} className="mb-3 inline-flex items-center gap-1 text-sm text-slate-600 hover:text-slate-900">
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
  // New work on an opportunity belongs to its customer's owner: only while the customer is here.
  const canAddWork = canChange && !opportunity.lead.restricted;
  const open = opportunity.status === "open";
  // Won and lost deals keep the owner who closed them (the API refuses those too).
  const canChangeOwner = permissions.canAssign && canChange && open;
  const stages = pipeline.data?.stages ?? [opportunity.stage];
  const won = stages.find((s) => s.category === "won" && s.is_active);
  const lost = stages.find((s) => s.category === "lost" && s.is_active);
  const inNegotiation = open && isNegotiation(opportunity.stage);
  const fields = pipeline.data?.custom_fields ?? [];
  const stored = (opportunity.custom_fields ?? {}) as Record<string, unknown>;

  const confirmMove = (target: Stage, lostReason: string, negotiatedPrice?: string) => {
    setMoveError(null);
    move.mutate(
      { id: opportunity.id, title: opportunity.title, version: opportunity.version, target, lostReason, negotiatedPrice },
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
  const openMove = (target: Stage | null) => {
    setMoveError(null);
    setPending({ target });
  };

  const menu = permissions.canWrite
    ? [
        ...(canChangeOwner ? [{ key: "owner", label: "Change owner", onSelect: () => setOwnerDialog(true) }] : []),
        ...(archived
          ? [{ key: "restore", label: "Restore", onSelect: () => (lifecycle.reset(), setLifecycleDialog("restore")) }]
          : [{ key: "archive", label: "Delete (archive)", tone: "danger" as const, onSelect: () => (lifecycle.reset(), setLifecycleDialog("archive")) }]),
      ]
    : [];

  return (
    <>
      {back}
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="break-words text-xl font-semibold tracking-tight text-slate-900">{opportunity.title}</h1>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm text-slate-600">
            <span className="sr-only">Status:</span>
            <OutcomeBadge status={opportunity.status} />
            <span>
              <span className="sr-only">Stage: </span>
              <strong className="font-medium text-slate-900">
                <StageName stage={opportunity.stage} />
              </strong>
            </span>
            <span className="min-w-0 truncate">{opportunity.account_name}</span>
            <span>
              <span className="sr-only">Owner: </span>
              <PersonName person={opportunity.owner} />
            </span>
          </p>
        </div>
        {permissions.canWrite ? (
          <div className="flex flex-wrap items-center gap-2">
            {canChange && open && won ? (
              <Button variant="secondary" size="sm" onClick={() => openMove(won)}>
                Won
              </Button>
            ) : null}
            {canChange && open && lost ? (
              <Button variant="secondary" size="sm" onClick={() => openMove(lost)}>
                Lost
              </Button>
            ) : null}
            {canChange ? (
              <Button variant="secondary" size="sm" icon={<ArrowRight aria-hidden="true" className="size-4" />} onClick={() => openMove(null)}>
                {open ? "Move" : "Reopen"}
              </Button>
            ) : null}
            {canChange ? (
              <Button size="sm" icon={<Pencil aria-hidden="true" className="size-4" />} onClick={() => setEditing(true)}>
                Edit
              </Button>
            ) : null}
            {menu.length ? <ActionMenu label={`More actions for ${opportunity.title}`} actions={menu} /> : null}
          </div>
        ) : null}
      </header>

      <div aria-live="polite" className="mb-3 empty:hidden">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
      </div>
      {archived ? (
        <div className="mb-3">
          <Alert tone="info" title="Archived">
            {formatDateTime(opportunity.archived_at)}. Hidden from the pipeline and its totals.
          </Alert>
        </div>
      ) : null}

      <Tabs tab={tab} onChange={setTab} />

      <div role="tabpanel" id={`deal-panel-${tab}`} aria-labelledby={`deal-tab-${tab}`} tabIndex={0} className="mt-4 focus:outline-none">
        {tab === "overview" ? (
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="space-y-4 lg:col-span-2">
              <Section
                title="Deal"
                actions={
                  inNegotiation && canChange ? (
                    <Button variant="secondary" size="sm" onClick={() => setPriceDialog(true)}>
                      Update price
                    </Button>
                  ) : null
                }
              >
                <Fields
                  items={[
                    ["Installation price", <Amount key="v" value={opportunity.value} className="font-semibold" />],
                    ...(opportunity.negotiated_price
                      ? ([
                          [
                            "Negotiated price",
                            <span key="n">
                              <Amount value={opportunity.negotiated_price} className="font-semibold" />{" "}
                              <span className="text-xs text-slate-500">
                                <When iso={opportunity.negotiated_at} />
                              </span>
                            </span>,
                          ],
                        ] as [string, ReactNode][])
                      : []),
                    [
                      "Probability",
                      <span key="p">
                        {formatPercent(opportunity.probability)}
                        {opportunity.probability_overridden ? <span className="text-slate-500"> (own)</span> : null}
                      </span>,
                    ],
                    ["Weighted value", <Amount key="w" value={opportunity.weighted_value} />],
                    ["Opportunity date", formatDateOnly(opportunity.opportunity_date)],
                    ["Expected closing", <CloseDate key="c" date={opportunity.expected_close_date} open={open} today={businessToday()} />],
                    ["Pipeline", opportunity.pipeline.name],
                    ...(opportunity.closed_at ? ([["Closed", <When key="cl" iso={opportunity.closed_at} />]] as [string, ReactNode][]) : []),
                    ...(opportunity.status === "lost" ? ([["Lost reason", opportunity.lost_reason || null]] as [string, ReactNode][]) : []),
                  ]}
                />
              </Section>
              <Section title="Customer">
                <Fields
                  items={[
                    ["Account", opportunity.account_name],
                    ["Customer", opportunity.customer_name],
                    // Its lead (the customer record made with it): read-only, one click away.
                    // Not linked when it has moved to someone else's workspace.
                    [
                      "Lead",
                      opportunity.lead.restricted || !opportunity.lead.id ? (
                        <span key="l" className="italic text-slate-500">In another workspace</span>
                      ) : (
                        <Link key="l" href={leadHref(workspace, opportunity.lead.id)} className="text-brand-700 [overflow-wrap:anywhere] hover:underline">
                          {opportunity.lead.display_name}
                        </Link>
                      ),
                    ],
                    [
                      "Phone",
                      opportunity.contact_phone ? (
                        <a key="t" href={telHref(opportunity.contact_phone)} className="text-brand-700 hover:underline">
                          {opportunity.contact_phone}
                        </a>
                      ) : null,
                    ],
                    [
                      "Email",
                      opportunity.contact_email ? (
                        <a key="e" href={mailtoHref(opportunity.contact_email)} className="break-all text-brand-700 hover:underline">
                          {opportunity.contact_email}
                        </a>
                      ) : null,
                    ],
                    ["Address", opportunity.address ? <span key="a" className="whitespace-pre-line">{opportunity.address}</span> : null],
                  ]}
                />
              </Section>
              <Section title="Instrument">
                <Fields
                  items={[
                    ["Instrument", opportunity.instrument_name || null],
                    ["Work load", opportunity.work_load || null],
                    ["Expected CPT", opportunity.expected_cpt || null],
                  ]}
                />
              </Section>
              {fields.length ? (
                <Section title="More details">
                  <Fields items={fields.map((field) => [field.name, formatCustomValue(field, stored[field.id])] as [string, ReactNode])} />
                </Section>
              ) : null}
              {opportunity.description ? (
                <Section title="Description">
                  <p className="whitespace-pre-line break-words text-sm text-slate-900">{opportunity.description}</p>
                </Section>
              ) : null}
            </div>
            <div className="space-y-4">
              {opportunity.lead.restricted ? null : (
                <CurrentWork workspace={workspace} target={{ opportunity: opportunity.id }} label={opportunity.title} canWrite={canAddWork} />
              )}
              <Section title="Record">
                <Fields
                  items={[
                    ["Owner", <PersonName key="ow" person={opportunity.owner} />],
                    ["Created by", <PersonName key="cb" person={opportunity.created_by} />],
                    ["Created", <When key="ca" iso={opportunity.created_at} />],
                    ["Updated", <When key="ua" iso={opportunity.updated_at} />],
                  ]}
                />
              </Section>
            </div>
          </div>
        ) : tab === "notes" ? (
          <DealNotes workspace={workspace} opportunityId={opportunity.id} canAdd={canAddWork} />
        ) : (
          <div className="space-y-4">
            <NegotiationHistory workspace={workspace} opportunityId={opportunity.id} version={opportunity.version} />
            <StageHistory workspace={workspace} opportunityId={opportunity.id} version={opportunity.version} />
            <Timeline workspace={workspace} subject={{ kind: "opportunity", id: opportunity.id }} />
          </div>
        )}
      </div>

      {editing && canChange ? (
        <OpportunityDrawer
          workspace={workspace}
          opportunity={opportunity}
          onClose={() => {
            setEditing(false);
            if (editOnOpen) router.replace(opportunityHref(workspace, opportunity.id));
          }}
          onSaved={() => {
            setEditing(false);
            if (editOnOpen) {
              // The /edit route is another page: the notice travels with the navigation.
              const href = opportunityHref(workspace, opportunity.id);
              setFlash("Changes saved.", href);
              router.replace(href);
            } else setNotice("Changes saved.");
          }}
        />
      ) : null}
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
      {ownerDialog ? (
        <OwnerDialog
          workspace={workspace}
          opportunity={opportunity}
          onClose={() => setOwnerDialog(false)}
          onSaved={(updated) => {
            setOwnerDialog(false);
            const message = `“${updated.title}” now belongs to ${updated.owner.full_name}.`;
            const owner = workspace.kind === "user" ? workspace.userId : workspace.kind === "self" ? viewer?.id : undefined;
            if (owner === undefined || updated.owner.id === owner) {
              sync(updated);
              setNotice(message);
              return;
            }
            // Handed out of this user's workspace: here it would only be found gone now.
            // Mark this workspace's data stale without refetching what this page shows, and
            // go to the Pipeline, which loads afresh.
            for (const queryKey of [pipelineKeys.all, activityKeys.all, timelineKeys.all, leadKeys.all]) {
              void queryClient.invalidateQueries({ queryKey, refetchType: "none" });
            }
            const segment = workspaceApiSegment(workspace);
            // Its lead (the customer record) moved with it (ADR-0028: the lead page would
            // otherwise show it here from the cache: frontend review).
            const lead = opportunity.lead.id;
            leftBehind.current = [
              ...(lead ? [leadKeys.detail(workspace, lead), ["leads", "opportunities", segment, lead]] : []),
              ["pipeline", "detail", segment, updated.id],
              ["pipeline", "history", segment, updated.id],
              ["pipeline", "negotiation", segment, updated.id],
              timelineKeys.subject(workspace, { kind: "opportunity", id: updated.id }),
              ["activities", "current", segment, updated.id],
              activityKeys.dealNotes(workspace, updated.id),
            ];
            const href = workspaceHref(workspace, "pipeline");
            setFlash(message, href);
            router.replace(href);
          }}
        />
      ) : null}
      {priceDialog ? (
        <PriceDialog
          workspace={workspace}
          opportunity={opportunity}
          onClose={() => setPriceDialog(false)}
          onSaved={(updated) => {
            sync(updated);
            setPriceDialog(false);
            setNotice("Negotiated price recorded.");
          }}
        />
      ) : null}
      <ConfirmDialog
        open={lifecycleDialog !== null}
        title={lifecycleDialog === "archive" ? "Delete this opportunity?" : "Restore this opportunity?"}
        confirmLabel={lifecycleDialog === "archive" ? "Delete" : "Restore"}
        tone={lifecycleDialog === "archive" ? "danger" : "primary"}
        busy={lifecycle.isPending}
        error={
          lifecycle.isError
            ? isApiError(lifecycle.error, 409)
              ? { message: "It changed a moment ago. The latest details are shown now; try again.", requestId: null }
              : describeError(lifecycle.error)
            : null
        }
        onConfirm={() => {
          const kind = lifecycleDialog === "archive" ? "archive" : "restore";
          lifecycle.mutate(kind, {
            onSuccess: () => {
              setLifecycleDialog(null);
              setNotice(kind === "archive" ? "Opportunity deleted (archived). You can restore it." : "Opportunity restored.");
            },
          });
        }}
        onCancel={() => setLifecycleDialog(null)}
      >
        {lifecycleDialog === "archive"
          ? "It's archived: removed from the pipeline and its totals. Its history is kept and it can be restored."
          : "It returns to the pipeline (and its totals, if open)."}
      </ConfirmDialog>
    </>
  );
}

/** The deal page's tabs (arrow keys, Home and End move between them). */
function Tabs({ tab, onChange }: { tab: Tab; onChange: (tab: Tab) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const index = TABS.findIndex((t) => t.id === tab);
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const last = TABS.length - 1;
    const next =
      event.key === "ArrowRight" ? (index === last ? 0 : index + 1) : event.key === "ArrowLeft" ? (index === 0 ? last : index - 1) : event.key === "Home" ? 0 : event.key === "End" ? last : null;
    if (next === null) return;
    event.preventDefault();
    onChange(TABS[next]!.id);
    refs.current[next]?.focus();
  };
  return (
    <div role="tablist" aria-label="Opportunity" className="flex gap-1 border-b border-slate-200">
      {TABS.map((t, i) => {
        const active = t.id === tab;
        return (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="tab"
            id={`deal-tab-${t.id}`}
            aria-selected={active}
            aria-controls={active ? `deal-panel-${t.id}` : undefined}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(t.id)}
            onKeyDown={onKeyDown}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
              active ? "border-brand-600 text-brand-700" : "border-transparent text-slate-600 hover:text-slate-900"
            }`}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Change an open deal's owner. The owner is the customer's (ADR-0027), so the customer's
 * other open deals and current work (open tasks, scheduled meetings, notes) move to them too;
 * won and lost deals, and completed or cancelled work, keep the owner who had them.
 */
function OwnerDialog({
  workspace,
  opportunity,
  onClose,
  onSaved,
}: {
  workspace: Workspace;
  opportunity: Opportunity;
  onClose: () => void;
  onSaved: (opportunity: Opportunity) => void;
}) {
  const queryClient = useQueryClient();
  const [owner, setOwner] = useState("");
  const [ownerLabel, setOwnerLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (to: string) => pipelineApi.assign(workspace, opportunity.id, to, opportunity.version),
    onSuccess: onSaved,
    // Someone changed the deal meanwhile: load it, so a retry sends its new version.
    onError: (failure) => {
      if (isApiError(failure, 409)) void queryClient.invalidateQueries({ queryKey: pipelineKeys.detail(workspace, opportunity.id) });
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!owner) {
      setError("Choose the new owner.");
      return;
    }
    if (owner === opportunity.owner.id) {
      onClose();
      return;
    }
    setError(null);
    save.mutate(owner);
  };
  const server = fieldErrors(save.error);
  const problem = save.isError && !server.owner ? describeError(save.error) : null;
  return (
    <Dialog
      open
      title="Change owner"
      description={
        <p>
          {opportunity.title} · now <PersonName person={opportunity.owner} />
        </p>
      }
      onClose={onClose}
      busy={save.isPending}
      size="sm"
    >
      <form onSubmit={submit} noValidate className="space-y-4">
        {problem ? (
          <Alert tone="error" requestId={problem.requestId}>
            {isApiError(save.error, 409) ? "It changed a moment ago. The latest version is loaded now: try again." : problem.message}
          </Alert>
        ) : null}
        <OwnerSelect
          label="New owner"
          name="owner"
          placeholder="Choose an owner"
          value={owner}
          valueLabel={ownerLabel}
          onChange={(id, label) => {
            setOwner(id);
            setOwnerLabel(label);
            setError(null);
          }}
          errors={error ? [error] : server.owner}
          autoFocus
        />
        <p className="text-sm text-slate-600">
          The customer&apos;s other open opportunities, open tasks, scheduled meetings and notes move to the new owner too. Won and lost
          opportunities, and completed or cancelled work, keep their owner.
        </p>
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            Change owner
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}

/** Record a new negotiated price while in a negotiation stage (appended to the history). */
function PriceDialog({
  workspace,
  opportunity,
  onClose,
  onSaved,
}: {
  workspace: Workspace;
  opportunity: Opportunity;
  onClose: () => void;
  onSaved: (opportunity: Opportunity) => void;
}) {
  const queryClient = useQueryClient();
  const [price, setPrice] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (value: string) => pipelineApi.recordNegotiatedPrice(workspace, opportunity.id, opportunity.version, value),
    onSuccess: onSaved,
    // Someone changed the deal meanwhile: load it, so a retry sends its new version.
    onError: (failure) => {
      if (isApiError(failure, 409)) void queryClient.invalidateQueries({ queryKey: pipelineKeys.detail(workspace, opportunity.id) });
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const parsed = parseAmountInput(price);
    if (!parsed.ok) {
      setError(price.trim() ? parsed.error : "Enter the negotiated price.");
      return;
    }
    setError(null);
    save.mutate(parsed.value);
  };
  const server = fieldErrors(save.error);
  const problem = save.isError && !server.price ? describeError(save.error) : null;
  return (
    <Dialog open title="Update negotiated price" description={<p>{opportunity.title}</p>} onClose={onClose} busy={save.isPending} size="sm">
      <form onSubmit={submit} noValidate className="space-y-4">
        {problem ? (
          <Alert tone="error" requestId={problem.requestId}>
            {isApiError(save.error, 409) ? "It changed a moment ago. The latest version is loaded now: check and save again." : problem.message}
          </Alert>
        ) : null}
        <TextField
          label="Negotiated price (₹)"
          name="price"
          inputMode="decimal"
          autoComplete="off"
          value={price}
          onChange={(e) => {
            setPrice(e.target.value);
            setError(null);
          }}
          errors={error ? [error] : server.price}
          data-autofocus
        />
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            Save price
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}

/** Every negotiated price, newest first (append-only: nothing is overwritten). */
function NegotiationHistory({ workspace, opportunityId, version }: { workspace: Workspace; opportunityId: string; version: number }) {
  const history = useQuery({
    queryKey: [...pipelineKeys.negotiation(workspace, opportunityId), version],
    queryFn: () => pipelineApi.negotiatedPrices(workspace, opportunityId),
    placeholderData: (previous) => previous,
  });
  const rows = history.data?.results;
  if (rows && rows.length === 0) return null;
  return (
    <Section title="Negotiated prices">
      {history.isError ? (
        <p className="text-sm text-red-700">The prices couldn&apos;t be loaded. {describeError(history.error).message}</p>
      ) : !rows ? (
        <Skeleton className="h-12 w-full" />
      ) : (
        <ol className="space-y-2">
          {rows.map((row) => (
            <li key={row.id} className="flex flex-wrap items-baseline justify-between gap-x-4 border-l-2 border-amber-300 pl-3 text-sm">
              <span className="font-semibold text-slate-900">
                <Amount value={row.price} />
              </span>
              <span className="text-xs text-slate-500">
                {row.stage_name} · <PersonName person={row.actor} /> · <time dateTime={row.occurred_at}>{formatDateTime(row.occurred_at)}</time>
              </span>
            </li>
          ))}
        </ol>
      )}
    </Section>
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
        <p className="text-sm text-slate-500">No stage changes yet.</p>
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
