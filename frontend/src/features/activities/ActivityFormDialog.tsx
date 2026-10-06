"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { useSingleFlight } from "@/components/ui/useSingleFlight";
import { DealPicker } from "@/features/pipeline/DealPicker";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Activity, ActivityCreateRequest } from "@/lib/api/types";
import { randomUuid } from "@/lib/random";
import type { Workspace } from "@/lib/workspace";

import { activitiesApi, activityKeys } from "./api";
import type { Schedule } from "./calendar";
import {
  changedFields,
  createRequest,
  type Draft,
  draftFromActivity,
  EMPTY_DRAFT,
  endAfterStartChange,
  FIELD_LABELS,
  type FormKind,
  mergeConflict,
  type Problems,
  updateRequest,
  validateDraft,
} from "./draft";
import { useActivityWriteSync } from "./hooks";

export type FormMode =
  | {
      kind: "create";
      /** Fixed when opened from an opportunity's page. */
      opportunity?: { id: string; label: string };
      /** The day or time chosen on the calendar (a prefilled form closes without asking). */
      schedule?: Schedule;
    }
  | { kind: "edit"; activity: Activity };

const KNOWN_FIELDS = new Set(["opportunity", "title", "description", "priority", "due_at", "starts_at", "ends_at", "location", "meeting_url"]);

/**
 * Create or edit a task or a meeting, in a dialog over the page it was opened from. It is
 * about an opportunity (ADR-0027) and belongs to its customer's owner; nobody picks an
 * owner. A create retried with exactly the same request reuses its idempotency key (a
 * timeout can't create two); an edit carries
 * the version it started from, and a conflict (409) keeps the typing on screen and offers to
 * apply it to the latest version. With `onKindChange` (a new entry from the calendar) the
 * form can switch between a meeting and a task, keeping what was typed.
 */
export function ActivityFormDialog({
  workspace,
  kind,
  mode,
  onClose,
  onSaved,
  onKindChange,
}: {
  workspace: Workspace;
  kind: FormKind;
  mode: FormMode;
  onClose: () => void;
  onSaved: (activity: Activity) => void;
  onKindChange?: (kind: FormKind) => void;
}) {
  const queryClient = useQueryClient();
  const sync = useActivityWriteSync(workspace);
  const form = useRef<HTMLFormElement>(null);
  const editing = mode.kind === "edit" ? mode.activity : null;
  const [base, setBase] = useState<Draft>(() =>
    editing ? draftFromActivity(editing) : { ...EMPTY_DRAFT, ...(mode.kind === "create" ? mode.schedule : undefined) },
  );
  const [version, setVersion] = useState(editing?.version ?? 0);
  const [draft, setDraft] = useState<Draft>(base);
  const [opportunity, setOpportunity] = useState(mode.kind === "create" ? (mode.opportunity?.id ?? "") : "");
  const [opportunityLabel, setOpportunityLabel] = useState(mode.kind === "create" ? (mode.opportunity?.label ?? "") : "");
  const [clientErrors, setClientErrors] = useState<Problems>({});
  const [conflict, setConflict] = useState<Activity | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const [reviewFields, setReviewFields] = useState<string[]>([]);
  // Asked before unsaved typing is thrown away (Escape, a click outside, Close, Cancel).
  const [discarding, setDiscarding] = useState(false);
  const idempotency = useRef<{ body: string; key: string } | null>(null);
  const keyFor = (body: ActivityCreateRequest): string => {
    const serialised = JSON.stringify(body);
    if (idempotency.current?.body !== serialised) idempotency.current = { body: serialised, key: randomUuid() };
    return idempotency.current.key;
  };

  const fixedOpportunity = mode.kind === "create" && Boolean(mode.opportunity);

  const save = useMutation({
    mutationFn: () => {
      if (editing) return activitiesApi.update(workspace, editing.id, updateRequest(kind, base, draft, version));
      const body = createRequest(kind, draft, opportunity);
      return activitiesApi.create(workspace, body, keyFor(body));
    },
    onSuccess: (activity) => sync(activity),
    onError: async (error) => {
      if (!editing || !isApiError(error, 409)) return;
      try {
        const latest = await activitiesApi.get(workspace, editing.id);
        queryClient.setQueryData(activityKeys.detail(workspace, editing.id), latest);
        if (latest.archived_at || (latest.status !== "open" && latest.status !== "scheduled")) {
          // Not editable any more (archived, completed or cancelled meanwhile): say so here,
          // keeping the typing on screen, rather than swapping the form away.
          setBlocked(latest.archived_at ? "archived" : (latest.status ?? "changed"));
          return;
        }
        setConflict(latest);
      } catch {
        setBlocked("unavailable");
      }
    },
  });

  useEffect(() => {
    if (conflict) form.current?.querySelector<HTMLElement>("[data-conflict-apply]")?.focus();
  }, [conflict]);
  useEffect(() => {
    if (discarding) form.current?.querySelector<HTMLElement>("[data-discard-keep]")?.focus();
  }, [discarding]);

  const server = fieldErrors(save.error);
  const errors: Record<string, readonly string[] | undefined> = { ...server, ...clientErrors };
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : save.error);
  const set = (field: keyof Draft) => (value: string) => setDraft((d) => ({ ...d, [field]: value }));
  const focusLater = (selector: string) => requestAnimationFrame(() => form.current?.querySelector<HTMLElement>(selector)?.focus());

  const opportunityChosen = mode.kind === "create" && !mode.opportunity && Boolean(opportunity);
  const dirty = changedFields(kind, base, draft).length > 0 || opportunityChosen;
  const requestClose = () => {
    if (!dirty) onClose();
    else setDiscarding(true);
  };
  const keepEditing = () => {
    setDiscarding(false);
    focusLater("[data-autofocus]");
  };

  const once = useSingleFlight();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    if (conflict) {
      focusLater("[data-conflict-apply]");
      return;
    }
    const problems = validateDraft(kind, draft, { requireOpportunity: mode.kind === "create", opportunity });
    setClientErrors(problems);
    if (Object.keys(problems).length) return;
    if (editing && changedFields(kind, base, draft).length === 0) {
      onClose();
      return;
    }
    // Guarded synchronously (`save.isPending` is seen a tick late): a second edit would carry
    // the same version and show a conflict with this one; a second create, the same reply.
    once(() => save.mutateAsync(undefined, { onSuccess: onSaved }));
  };

  const applyMine = () => {
    if (!conflict) return;
    const latest = draftFromActivity(conflict);
    const { merged, overlapping } = mergeConflict(kind, base, draft, latest);
    setBase(latest);
    setDraft(merged);
    setVersion(conflict.version);
    setReviewFields(overlapping.map((f) => FIELD_LABELS[f] ?? f));
    setConflict(null);
    save.reset();
    focusLater(overlapping.length ? `[name="${overlapping[0]}"]` : 'button[type="submit"]');
  };

  const discardMine = () => {
    if (!conflict) return;
    const latest = draftFromActivity(conflict);
    setBase(latest);
    setDraft(latest);
    setVersion(conflict.version);
    setReviewFields([]);
    setConflict(null);
    save.reset();
    focusLater('button[type="submit"]');
  };

  const switchKind = (next: FormKind) => {
    if (next === kind || save.isPending) return;
    // The other type's fields have their own problems (and the server's answer was about
    // the other request).
    setClientErrors({});
    save.reset();
    onKindChange?.(next);
  };

  const noun = kind === "task" ? "task" : "meeting";
  // An edit's 409 has its own banner (above); a create's 409 (a retried request whose task
  // has since left this workspace) is explained by the server's message.
  const unmapped = save.isError && !(editing && isApiError(save.error, 409)) && !Object.keys(server).some((f) => KNOWN_FIELDS.has(f));
  const banner = describeError(save.error);
  const title = editing ? `Edit ${noun}` : kind === "task" ? "New task" : "Schedule a meeting";

  return (
    <Dialog open title={title} onClose={requestClose} busy={save.isPending}>
      <form ref={form} onSubmit={submit} noValidate className="space-y-4">
        {discarding ? (
          <Alert
            tone="error"
            title="Discard what you typed?"
            action={
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="secondary" onClick={keepEditing} data-discard-keep>
                  Keep editing
                </Button>
                <Button size="sm" variant="danger" onClick={onClose}>
                  Discard
                </Button>
              </div>
            }
          >
            Your changes haven&apos;t been saved.
          </Alert>
        ) : null}
        {conflict ? (
          <Alert
            tone="error"
            title={`Someone else changed this ${noun} while you were editing`}
            action={
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={applyMine} data-conflict-apply>
                  Apply my changes to the latest version
                </Button>
                <Button size="sm" variant="secondary" onClick={discardMine}>
                  Discard my changes
                </Button>
              </div>
            }
          >
            Your changes haven&apos;t been saved yet. Nothing was overwritten.
          </Alert>
        ) : blocked ? (
          <Alert tone="error" title={`This ${noun} can't be edited now`}>
            {blocked === "unavailable"
              ? "Someone else changed it, and its latest version couldn't be loaded. "
              : `Someone else ${blocked === "archived" ? "archived" : `marked it ${blocked}`} while you were editing. `}
            Your changes are still in the form, not saved; copy anything you need before closing.
          </Alert>
        ) : unmapped ? (
          <Alert tone="error" requestId={banner.requestId}>
            {server.non_field_errors?.join(" ") ?? banner.message}
          </Alert>
        ) : null}
        {reviewFields.length ? (
          <Alert tone="info" title="Review before saving">
            Your changes were applied to the latest version. These fields were also changed by someone else; your values are kept:{" "}
            {reviewFields.join(", ")}.
          </Alert>
        ) : null}

        {onKindChange && !editing ? (
          <div role="group" aria-label="What to schedule" className="inline-flex rounded-md border border-slate-300 bg-white p-0.5 text-sm">
            {(["meeting", "task"] as const).map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={kind === option}
                onClick={() => switchKind(option)}
                className={`rounded px-3 py-1 ${kind === option ? "bg-slate-900 font-medium text-white" : "text-slate-600 hover:bg-slate-50"}`}
              >
                {option === "meeting" ? "Meeting" : "Task"}
              </button>
            ))}
          </div>
        ) : null}

        {editing ? (
          <p className="text-sm text-slate-600">
            About:{" "}
            <strong className="font-medium text-slate-900">
              {editing.opportunity && !editing.opportunity.restricted
                ? editing.opportunity.title
                : editing.lead.restricted
                  ? "A customer in another workspace"
                  : editing.lead.display_name}
            </strong>
          </p>
        ) : fixedOpportunity && mode.kind === "create" ? (
          <p className="text-sm text-slate-600">
            About: <strong className="font-medium text-slate-900">{mode.opportunity?.label}</strong>
          </p>
        ) : (
          <DealPicker
            workspace={workspace}
            value={opportunity}
            valueLabel={opportunityLabel}
            onChange={(id, label) => {
              setOpportunity(id);
              setOpportunityLabel(label);
            }}
            placeholder="Choose an opportunity"
            errors={errors.opportunity}
          />
        )}

        <TextField
          label="Subject"
          name="title"
          value={draft.title}
          maxLength={200}
          onChange={(e) => set("title")(e.target.value)}
          errors={errors.title}
          autoComplete="off"
          data-autofocus
        />

        {kind === "task" ? (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <TextField
                label="Due date"
                name="due_at"
                type="date"
                optional
                min="2000-01-01"
                max="2099-12-31"
                value={draft.dueDate}
                onChange={(e) => set("dueDate")(e.target.value)}
                errors={errors.due_at}
              />
              <TextField
                label="Due time"
                name="due_time"
                type="time"
                value={draft.dueTime}
                disabled={!draft.dueDate}
                onChange={(e) => set("dueTime")(e.target.value)}
                hint="India time (IST)."
              />
            </div>
            <SelectField
              label="Priority"
              name="priority"
              value={draft.priority}
              onChange={(e) => set("priority")(e.target.value)}
              options={[
                { value: "low", label: "Low" },
                { value: "normal", label: "Normal" },
                { value: "high", label: "High" },
              ]}
              errors={errors.priority}
            />
          </>
        ) : (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <TextField
                label="Start"
                name="starts_at"
                type="datetime-local"
                value={draft.startsAt}
                onChange={(e) => {
                  const startsAt = e.target.value;
                  // The end moves with the start (the meeting keeps its length).
                  setDraft((d) => ({ ...d, startsAt, endsAt: endAfterStartChange(d.startsAt, d.endsAt, startsAt) }));
                }}
                errors={errors.starts_at}
                hint="India time (IST)."
              />
              <TextField
                label="End"
                name="ends_at"
                type="datetime-local"
                value={draft.endsAt}
                onChange={(e) => set("endsAt")(e.target.value)}
                errors={errors.ends_at}
              />
            </div>
            <TextField
              label="Location"
              name="location"
              optional
              maxLength={200}
              value={draft.location}
              onChange={(e) => set("location")(e.target.value)}
              errors={errors.location}
            />
            <TextField
              label="Meeting link"
              name="meeting_url"
              type="url"
              optional
              maxLength={500}
              placeholder="https://"
              value={draft.meetingUrl}
              onChange={(e) => set("meetingUrl")(e.target.value)}
              errors={errors.meeting_url}
            />
          </>
        )}

        <TextAreaField
          label={kind === "task" ? "Description" : "Agenda"}
          name="description"
          optional
          rows={4}
          maxLength={10000}
          value={draft.description}
          onChange={(e) => set("description")(e.target.value)}
          errors={errors.description}
        />

        <DialogActions>
          <Button variant="secondary" onClick={requestClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending} disabled={Boolean(blocked)}>
            {editing ? "Save changes" : kind === "task" ? "Create task" : "Schedule meeting"}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
