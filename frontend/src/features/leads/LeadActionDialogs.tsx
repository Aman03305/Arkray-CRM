"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Lead, LeadOptions } from "@/lib/api/types";
import type { Workspace } from "@/lib/workspace";

import { leadKeys, leadsApi } from "./api";
import { useLeadWriteSync } from "./hooks";
import { OwnerSelect } from "./OwnerSelect";

const STALE = "Someone else changed this lead a moment ago. The latest details are shown now; please check and try again.";

/** Caches stay in sync whatever happens to the dialog; `onDone` (which may navigate) only
 * runs while the dialog is still open. */
function useLeadMutation<T>(workspace: Workspace, lead: Lead, run: (value: T) => Promise<Lead>) {
  const queryClient = useQueryClient();
  const sync = useLeadWriteSync(workspace);
  return useMutation({
    mutationFn: run,
    onSuccess: sync,
    onError: (error) => {
      // Stale version: load the latest, so trying again starts from current data.
      if (isApiError(error, 409)) void queryClient.invalidateQueries({ queryKey: leadKeys.detail(workspace, lead.id) });
    },
  });
}

function errorMessage(error: unknown): { message: string; requestId: string | null } | null {
  if (!error) return null;
  if (isApiError(error, 409)) return { message: STALE, requestId: null };
  return describeError(error);
}

export function ChangeStatusDialog({ workspace, lead, options, onClose, onDone }: {
  workspace: Workspace;
  lead: Lead;
  options: LeadOptions | undefined;
  onClose: () => void;
  onDone: (lead: Lead) => void;
}) {
  const [status, setStatus] = useState(lead.status.key);
  const groupId = useId();
  const save = useLeadMutation(workspace, lead, (key: string) => leadsApi.changeStatus(workspace, lead.id, key, lead.version));
  const choices = (options?.statuses ?? []).filter((s) => s.is_active || s.key === lead.status.key);
  // Focus starts on the current status, or the first choosable one if it has been retired.
  const focusKey = choices.find((c) => c.key === status && c.is_active)?.key ?? choices.find((c) => c.is_active)?.key;
  const problem = errorMessage(save.error);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    if (status === lead.status.key) onClose();
    else save.mutate(status, { onSuccess: onDone });
  };

  return (
    <Dialog open title="Change status" description={`${lead.display_name} is currently ${lead.status.name}.`} onClose={onClose} busy={save.isPending} size="sm">
      <form onSubmit={onSubmit} className="space-y-4">
        {problem ? (
          <Alert tone="error" requestId={problem.requestId}>
            {fieldErrors(save.error).status?.join(" ") ?? problem.message}
          </Alert>
        ) : null}
        <fieldset>
          <legend id={groupId} className="sr-only">
            Status
          </legend>
          <div className="space-y-1">
            {choices.map((option) => (
              <label key={option.key} className="flex cursor-pointer items-center gap-3 rounded-md px-2 py-1.5 hover:bg-slate-50">
                <input
                  type="radio"
                  name="status"
                  value={option.key}
                  checked={status === option.key}
                  onChange={() => setStatus(option.key)}
                  disabled={!option.is_active}
                  data-autofocus={option.key === focusKey || undefined}
                  className="size-4 accent-brand-600"
                />
                <span className="text-sm text-slate-800">
                  {option.name}
                  {option.is_active ? null : <span className="ml-1 text-xs text-slate-400">(retired)</span>}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            Save status
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}

export function ReassignDialog({ workspace, lead, onClose, onDone }: {
  workspace: Workspace;
  lead: Lead;
  onClose: () => void;
  onDone: (lead: Lead) => void;
}) {
  const [owner, setOwner] = useState("");
  const [ownerLabel, setOwnerLabel] = useState("");
  const [clientError, setClientError] = useState<string | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const save = useLeadMutation(workspace, lead, (userId: string) => leadsApi.assign(workspace, lead.id, userId, lead.version));
  const serverOwnerError = fieldErrors(save.error).owner;
  const problem = serverOwnerError ? null : errorMessage(save.error);
  // Keyboard and screen-reader users land on the problem.
  useFocusFirstInvalid(form, clientError ?? serverOwnerError);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    if (!owner) {
      setClientError("Choose who should own this lead.");
      return;
    }
    setClientError(null);
    if (owner === lead.owner.id) onClose();
    else save.mutate(owner, { onSuccess: onDone });
  };

  return (
    <Dialog
      open
      title={`Reassign ${lead.display_name}`}
      description={
        <>
          <p>Currently owned by {lead.owner.full_name}.</p>
          <p>The new owner will see this lead in their workspace, and {lead.owner.full_name} will no longer see it. Its history is kept.</p>
        </>
      }
      onClose={onClose}
      busy={save.isPending}
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {problem ? (
          <Alert tone="error" requestId={problem.requestId}>
            {problem.message}
          </Alert>
        ) : null}
        <OwnerSelect
          label="New owner"
          placeholder="Choose a user"
          value={owner}
          valueLabel={ownerLabel}
          onChange={(id, label) => {
            setOwner(id);
            setOwnerLabel(label);
          }}
          errors={clientError ? [clientError] : serverOwnerError}
          autoFocus
        />
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            Reassign lead
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
