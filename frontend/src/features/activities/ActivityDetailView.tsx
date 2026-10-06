"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowLeft, ExternalLink, Pencil } from "lucide-react";
import Link from "next/link";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { Skeleton } from "@/components/ui/Skeleton";
import { PersonName } from "@/components/ui/PersonName";
import { useSingleFlight } from "@/components/ui/useSingleFlight";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Activity } from "@/lib/api/types";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useViewer } from "@/lib/viewer-context";
import { sectionBack, type Workspace, workspaceHref } from "@/lib/workspace";

import { activitiesApi, activityKeys, type LifecycleAction } from "./api";
import { CustomerName, OpportunityLink, PriorityLabel, StatusBadge, TypeLabel, typeLabel } from "./ActivityBits";
import { ActivityFormDialog } from "./ActivityFormDialog";
import { canComplete, useClock } from "./clock";
import { activityPermissions, canEditNote, useActivityAction, useActivityWriteSync } from "./hooks";

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
    <dl className="grid gap-x-4 gap-y-3 text-sm sm:grid-cols-[9rem_1fr]">
      {items.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-slate-500">{label}</dt>
          <dd className="min-w-0 break-words text-slate-900">{value ?? <span className="text-slate-500">—</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

function When({ iso }: { iso: string | null }) {
  if (!iso) return null;
  return (
    <time dateTime={iso}>
      {formatDateTime(iso)} <span className="text-slate-500">({formatRelative(iso)})</span>
    </time>
  );
}

/** A meeting link: only ever an https:// URL (the server refuses anything else), opened in
 * a new tab without giving that page a handle on this one. */
function MeetingLink({ url }: { url: string }) {
  if (!url || !url.toLowerCase().startsWith("https://")) return null;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 break-all text-brand-700 hover:underline">
      {url}
      <ExternalLink aria-hidden="true" className="size-3.5 shrink-0" />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  );
}

const ACTION_DONE: Record<LifecycleAction, string> = {
  complete: "Marked as completed.",
  cancel: "Cancelled.",
  reopen: "Reopened.",
  archive: "Archived.",
  restore: "Restored.",
};

export function ActivityDetailView({ workspace, activityId }: { workspace: Workspace; activityId: string }) {
  const viewer = useViewer();
  const queryClient = useQueryClient();
  const permissions = activityPermissions(viewer, workspace);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<{ message: string; requestId: string | null } | null>(null);
  const [confirm, setConfirm] = useState<"cancel" | "archive" | null>(null);
  const [editing, setEditing] = useState(false);
  // After an action the button that had focus may be gone (Complete, Reopen): focus moves
  // to the message saying what happened.
  const noticeRegion = useRef<HTMLDivElement>(null);
  const focusNotice = useRef(false);
  useEffect(() => {
    if (focusNotice.current && (notice || problem)) {
      focusNotice.current = false;
      noticeRegion.current?.focus();
    }
  }, [notice, problem]);
  const clock = useClock();
  const detail = useQuery({
    queryKey: activityKeys.detail(workspace, activityId),
    queryFn: () => activitiesApi.get(workspace, activityId),
  });
  const action = useActivityAction(workspace);
  const once = useSingleFlight();

  const back = (
    <Link href={workspaceHref(workspace, "activities")} className="-mt-1 mb-3 inline-flex items-center gap-1 py-1 text-sm text-slate-600 hover:text-slate-900">
      <ArrowLeft aria-hidden="true" className="size-4" />
      Activities
    </Link>
  );

  if (isApiError(detail.error, 404)) return <NotFoundView back={sectionBack(workspace, "activities")} />;
  // A refusal (403) removes what was shown: the server no longer lets this viewer see it.
  if (detail.isError && (!detail.data || isApiError(detail.error, 403))) {
    const { message, requestId } = describeError(detail.error);
    return (
      <>
        {back}
        <Alert
          tone="error"
          title={isApiError(detail.error, 403) ? "You can't view this activity" : "This activity couldn't be loaded"}
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
  const activity = detail.data;
  if (!activity) {
    return (
      <div aria-busy="true" className="space-y-4">
        <Skeleton className="h-7 w-72" />
        <Skeleton className="h-40 w-full" />
        <span className="sr-only">Loading activity</span>
      </div>
    );
  }

  const archived = activity.archived_at !== null;
  const current = activity.status === "open" || activity.status === "scheduled";
  const note = activity.type === "note";
  const canChange = permissions.canWrite && !archived;
  const completable = canComplete(activity, clock);

  const run = (kind: LifecycleAction) => {
    // One action at a time: a second Complete would carry the same version and report
    // "someone else changed this" about the first.
    once(() => {
      setNotice(null);
      setProblem(null);
      return action.mutateAsync(
        { activity, action: kind },
        {
          onSuccess: () => {
            setConfirm(null);
            focusNotice.current = true;
            setNotice(ACTION_DONE[kind]);
          },
          onError: (error) => {
            setConfirm(null);
            focusNotice.current = true;
            if (isApiError(error, 409)) void queryClient.invalidateQueries({ queryKey: activityKeys.detail(workspace, activity.id) });
            setProblem(
              isApiError(error, 409)
                ? { message: "Someone else changed this a moment ago. The latest details are shown now; please try again.", requestId: null }
                : describeError(error),
            );
          },
        },
      );
    });
  };

  const heading = note ? "Note" : activity.title;

  return (
    <>
      {back}
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="mb-1 text-sm">
            <TypeLabel type={activity.type} />
          </p>
          <h1 className="break-words text-xl font-semibold tracking-tight text-slate-900">{heading}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-slate-600">
            {activity.status ? (
              <span>
                <span className="sr-only">Status: </span>
                <StatusBadge status={activity.status} overdue={activity.is_overdue} type={activity.type} />
              </span>
            ) : null}
            <span>
              {note ? "Written by" : "Owner"}:{" "}
              <strong className="font-medium text-slate-900">
                <PersonName person={note ? activity.created_by : activity.owner} />
              </strong>
            </span>
          </div>
        </div>
        {permissions.canWrite ? (
          <div className="flex flex-wrap items-center gap-2">
            {canChange && completable ? (
              <Button onClick={() => run("complete")} loading={action.isPending && action.variables?.action === "complete"}>
                {activity.type === "meeting" ? "Mark as completed" : "Complete"}
              </Button>
            ) : null}
            {canChange && !note && !current ? (
              <Button variant="secondary" onClick={() => run("reopen")} loading={action.isPending && action.variables?.action === "reopen"}>
                Reopen
              </Button>
            ) : null}
            {canChange && !note && current ? (
              <Button variant="secondary" onClick={() => { action.reset(); setConfirm("cancel"); }}>
                Cancel {typeLabel(activity.type).toLowerCase()}
              </Button>
            ) : null}
            {canChange && current && !note ? (
              <Button variant="secondary" icon={<Pencil aria-hidden="true" className="size-4" />} onClick={() => setEditing(true)}>
                Edit
              </Button>
            ) : null}
            {archived ? (
              <Button variant="secondary" onClick={() => run("restore")} loading={action.isPending && action.variables?.action === "restore"}>
                Restore
              </Button>
            ) : (
              <Button variant="ghost" icon={<Archive aria-hidden="true" className="size-4" />} onClick={() => { action.reset(); setConfirm("archive"); }}>
                Archive
              </Button>
            )}
          </div>
        ) : null}
      </header>

      <div ref={noticeRegion} tabIndex={-1} aria-live="polite" className="mb-4 space-y-2 empty:hidden focus:outline-none">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
        {problem ? (
          <Alert tone="error" requestId={problem.requestId}>
            {problem.message}
          </Alert>
        ) : null}
      </div>
      {archived ? (
        <div className="mb-4">
          <Alert tone="info" title="This activity is archived">
            Archived {formatDateTime(activity.archived_at)}. It is hidden from lists, timelines and counts
            {permissions.canWrite ? "; restore it to make changes" : ""}.
          </Alert>
        </div>
      ) : null}
      {activity.type === "meeting" && current && !completable && canChange ? (
        <p className="mb-4 text-sm text-slate-500">This meeting can be marked as completed once it has started.</p>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          {note ? (
            <NoteBody
              workspace={workspace}
              activity={activity}
              canEdit={canChange && canEditNote(viewer, activity)}
              onSaved={() => setNotice("Note saved.")}
            />
          ) : (
            <>
              <Section title="Details">
                <Fields
                  items={[
                    ["Customer", <CustomerName key="l" lead={activity.lead} />],
                    ["Opportunity", activity.opportunity ? <OpportunityLink key="o" workspace={workspace} opportunity={activity.opportunity} /> : null],
                    ...(activity.type === "task"
                      ? ([
                          ["Due", activity.due_at ? <When key="d" iso={activity.due_at} /> : "No due date"],
                          ["Priority", <PriorityLabel key="p" priority={activity.priority} />],
                        ] as [string, ReactNode][])
                      : ([
                          ["Starts", <When key="s" iso={activity.starts_at} />],
                          ["Ends", <When key="e" iso={activity.ends_at} />],
                          ["Location", activity.location || null],
                          ["Meeting link", activity.meeting_url ? <MeetingLink key="u" url={activity.meeting_url} /> : null],
                        ] as [string, ReactNode][])),
                  ]}
                />
              </Section>
              <Section title={activity.type === "task" ? "Description" : "Agenda"}>
                {activity.description ? (
                  <p className="whitespace-pre-line break-words text-sm text-slate-900">{activity.description}</p>
                ) : (
                  <p className="text-sm text-slate-500">Nothing written</p>
                )}
              </Section>
            </>
          )}
        </div>
        <div className="space-y-4">
          {note ? (
            <Section title="About">
              <Fields
                items={[
                  ["Customer", <CustomerName key="l" lead={activity.lead} />],
                  ["Opportunity", activity.opportunity ? <OpportunityLink key="o" workspace={workspace} opportunity={activity.opportunity} /> : null],
                ]}
              />
            </Section>
          ) : null}
          <Section title="Record details">
            <Fields
              items={[
                ["Created by", <PersonName key="cb" person={activity.created_by} />],
                ["Created", <When key="ca" iso={activity.created_at} />],
                ...(activity.completed_by
                  ? ([[activity.type === "meeting" ? "Held, recorded by" : "Completed by", <PersonName key="cpb" person={activity.completed_by} />],
                      ["Completed", <When key="cpa" iso={activity.completed_at} />]] as [string, ReactNode][])
                  : []),
                ...(activity.cancelled_by
                  ? ([["Cancelled by", <PersonName key="clb" person={activity.cancelled_by} />],
                      ["Cancelled", <When key="cla" iso={activity.cancelled_at} />]] as [string, ReactNode][])
                  : []),
                ...(note ? [] : ([["Owner", <PersonName key="ow" person={activity.owner} />]] as [string, ReactNode][])),
                ["Last updated", <When key="ua" iso={activity.updated_at} />],
              ]}
            />
          </Section>
        </div>
      </div>

      {editing && (activity.type === "task" || activity.type === "meeting") ? (
        <ActivityFormDialog
          workspace={workspace}
          kind={activity.type}
          mode={{ kind: "edit", activity }}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            setNotice("Changes saved.");
          }}
        />
      ) : null}
      <ConfirmDialog
        open={confirm !== null}
        title={confirm === "archive" ? `Archive this ${typeLabel(activity.type).toLowerCase()}?` : `Cancel ${heading}?`}
        confirmLabel={confirm === "archive" ? "Archive" : `Cancel ${typeLabel(activity.type).toLowerCase()}`}
        tone="danger"
        busy={action.isPending}
        error={action.isError ? describeError(action.error) : null}
        onConfirm={() => confirm && run(confirm)}
        onCancel={() => setConfirm(null)}
      >
        {confirm === "archive"
          ? "It will be hidden from lists, timelines and counts. Nothing is deleted, and it can be restored."
          : "It stays in the history as cancelled, with who cancelled it and when, and can be reopened."}
      </ConfirmDialog>
    </>
  );
}

/** A note's text, editable in place by its author only (the server enforces it too). */
function NoteBody({
  workspace,
  activity,
  canEdit,
  onSaved,
}: {
  workspace: Workspace;
  activity: Activity;
  canEdit: boolean;
  onSaved: () => void;
}) {
  const queryClient = useQueryClient();
  const sync = useActivityWriteSync(workspace);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(activity.description);
  const [version, setVersion] = useState(activity.version);
  // A save refused because the note changed meanwhile: edited again ("changed": the latest
  // text is shown under this one), archived (it can't be saved now), or not reloadable.
  const [conflict, setConflict] = useState<"changed" | "archived" | "unavailable" | null>(null);
  const fieldId = useId();
  // Saving swaps the form for the note: focus returns to "Edit note", not to the page.
  const editArea = useRef<HTMLDivElement>(null);
  const focusEdit = useRef(false);
  useEffect(() => {
    if (!editing && focusEdit.current) {
      focusEdit.current = false;
      editArea.current?.querySelector("button")?.focus();
    }
  }, [editing]);
  const save = useMutation({
    mutationFn: () => activitiesApi.update(workspace, activity.id, { version, description: text }),
    onSuccess: (saved) => {
      sync(saved);
      focusEdit.current = true;
      setEditing(false);
      setConflict(null);
      onSaved();
    },
    onError: async (error) => {
      if (!isApiError(error, 409)) return;
      // Someone changed it meanwhile: keep this text on screen, show the latest underneath.
      const latest = await activitiesApi.get(workspace, activity.id).catch(() => null);
      if (latest) {
        queryClient.setQueryData(activityKeys.detail(workspace, activity.id), latest);
        setVersion(latest.version);
      }
      setConflict(!latest ? "unavailable" : latest.archived_at ? "archived" : "changed");
    },
  });
  const once = useSingleFlight();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    // Not `save.isPending` (seen a tick late): a second save would carry the same version,
    // get a 409 and show a conflict with the author's own first save.
    once(() => save.mutateAsync());
  };
  const message = save.isError && !isApiError(save.error, 409) ? (fieldErrors(save.error).description?.join(" ") ?? describeError(save.error).message) : null;

  if (!editing) {
    return (
      <Section title="Note">
        <p className="whitespace-pre-line break-words text-sm text-slate-900">{activity.description}</p>
        {canEdit ? (
          <div ref={editArea} className="mt-4">
            <Button
              variant="secondary"
              size="sm"
              icon={<Pencil aria-hidden="true" className="size-4" />}
              onClick={() => {
                setText(activity.description);
                setVersion(activity.version);
                setEditing(true);
              }}
            >
              Edit note
            </Button>
          </div>
        ) : null}
      </Section>
    );
  }
  return (
    <Section title="Note">
      <form onSubmit={submit} noValidate className="space-y-3">
        {conflict === "archived" ? (
          <Alert tone="error" title="This note was archived while you were editing">
            Your text is still here, not saved; copy anything you need. It can be changed again once it is restored.
          </Alert>
        ) : conflict === "unavailable" ? (
          <Alert tone="error" title="Someone else changed this note while you were editing">
            Your text is still here, not saved. Its latest version couldn&apos;t be loaded; try saving again in a moment.
          </Alert>
        ) : conflict === "changed" ? (
          <Alert tone="error" title="Someone else changed this note while you were editing">
            Your text is still here, not saved. Their version is below; save again to replace it with yours, or cancel to keep theirs.
          </Alert>
        ) : null}
        <label htmlFor={fieldId} className="sr-only">
          Note text
        </label>
        <textarea
          id={fieldId}
          rows={8}
          maxLength={10000}
          value={text}
          // Read-only while saving: what is typed then would not be in the saved note.
          readOnly={save.isPending}
          aria-busy={save.isPending || undefined}
          onChange={(e) => setText(e.target.value)}
          aria-invalid={message ? true : undefined}
          aria-describedby={message ? `${fieldId}-error` : undefined}
          className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 read-only:bg-slate-50 focus-visible:outline-brand-600"
          autoFocus
        />
        {conflict === "changed" ? (
          <figure className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
            <figcaption className="mb-1 text-xs font-medium text-slate-600">Their version (saved)</figcaption>
            <p className="whitespace-pre-line break-words text-sm text-slate-900">{activity.description}</p>
          </figure>
        ) : null}
        {message ? (
          <p id={`${fieldId}-error`} role="alert" className="text-xs text-red-600">
            {message}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" size="sm" disabled={save.isPending} onClick={() => { focusEdit.current = true; setEditing(false); setConflict(null); save.reset(); }}>
            Cancel
          </Button>
          <Button type="submit" size="sm" loading={save.isPending} disabled={conflict === "archived"}>
            Save note
          </Button>
        </div>
      </form>
    </Section>
  );
}
